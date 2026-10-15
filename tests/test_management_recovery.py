from copy import deepcopy
import json
import time
import os
import hashlib
import multiprocessing

import ccxt
import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils import position_management as management
from backend.utils.trade_operations import run_once
from test_exclusive_position_management import ExclusiveExchange, make_service, SYMBOL


@pytest.fixture
def managed(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'recovery.db'))
    with database.get_db_conn() as conn:
        pass  # PostgreSQL migrations are installed by financial_pg.
    exchange = ExclusiveExchange()
    monkeypatch.setattr(time, 'time', lambda: exchange.now / 1000)
    service = make_service(exchange)
    management.ensure_managed(service, SYMBOL)
    return service, exchange


def reservation(service, operation_id, state='unresolved', side='LONG'):
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO position_management_actions(account_scope,symbol,side,owner,operation_id,state,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_scope,symbol,side) DO UPDATE SET owner=excluded.owner,operation_id=excluded.operation_id,state=excluded.state,updated_at=excluded.updated_at',
                     (service.account_scope, SYMBOL, side, service.config_id, operation_id, state, 0))
        conn.commit()


def reservation_state():
    with database.get_db_conn() as conn:
        return conn.execute('SELECT state FROM position_management_actions').fetchone()[0]


def predecessor_queue(service, exchange):
    current = service._load(SYMBOL, 'LONG')
    old = exchange.external_order('old-conditional', channel='algo', trigger=2458, price=None, dynamic=True)
    record = management._external_record({**old, '_channel': 'algo'}, 'LONG', 'exit', True)
    current['management'].update(previous_episode_id='confirmed-predecessor', pending_orders=[record])
    service._save(current)
    return current


def test_crashed_running_without_execution_proof_never_releases_by_age(managed):
    service, exchange = managed
    reservation(service, 'missing-original', state='running')
    exchange.now += 86400000
    assert management.recover_reservations(service, SYMBOL) == 0
    assert reservation_state() == 'running'
    assert exchange.writes == []


def test_read_only_recovery_resolves_lost_ack_using_original_identity(managed):
    service, exchange = managed
    exchange.timeout_create = True
    request = {'tool': 'close', 'side': 'LONG', 'amount': .01, 'order_type': 'limit', 'price': 2565}
    result = run_once(service.config_id, SYMBOL, 'original-close', request,
                      lambda: service.close(SYMBOL, 'LONG', .01, 'limit', price=2565,
                                            operation_id='original-close', contract_version=2))
    # Recreate the unresolved physical scope left by a worker crash after send.
    reservation(service, 'original-close')
    before = deepcopy(exchange.writes)
    assert management.recover_reservations(service, SYMBOL) == 1
    assert reservation_state() == 'finished'
    assert exchange.writes == before
    replay = run_once(service.config_id, SYMBOL, 'original-close', request,
                      lambda: pytest.fail('Recovery replayed financial callback'))
    assert replay['status'] in {'submitted', 'completed'}


def test_child_reservation_recovers_from_exact_parent_item(managed):
    service, exchange = managed
    child = 'original-batch:0'
    raw = service.close(SYMBOL, 'LONG', .01, 'limit', price=2565,
                        operation_id=child, contract_version=2)
    with database.get_db_conn() as conn:
        now = time.time()
        result = {'status': 'unknown', 'results': [{'index': 0, 'status': 'unknown', 'result': raw}]}
        conn.execute('INSERT INTO trade_action_runs (config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                     (service.config_id, 'original-batch', SYMBOL, 'saved-hash', 'unknown', json.dumps(result), now, now))
        conn.execute('INSERT INTO trade_operation_requests (config_id,operation_id,request_json) VALUES(%s,%s,%s)',
                     (service.config_id, 'original-batch', json.dumps({'tool': 'execute_trade_actions', 'actions': [{'action': 'close'}]})))
        conn.commit()
    reservation(service, child)
    before = deepcopy(exchange.writes)
    assert management.recover_reservations(service, SYMBOL) == 1
    assert reservation_state() == 'finished' and exchange.writes == before


def test_similar_prefix_cannot_borrow_another_receipt(managed):
    service, _ = managed
    reservation(service, 'original-other:0')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO trade_action_runs (config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                     (service.config_id, 'original', SYMBOL, 'hash', 'completed', '{"status":"completed"}', 0, 0))
        conn.commit()
    assert management.recover_reservations(service, SYMBOL) == 0
    assert reservation_state() == 'unresolved'


def test_cleanup_lost_ack_keeps_reservation_and_recovers_without_another_cancel(managed, monkeypatch):
    service, exchange = managed
    predecessor_queue(service, exchange)
    original_query = service._query
    original_cancel = exchange.cancel_order
    sent = []
    def lost_ack(*args, **kwargs):
        response = original_cancel(*args, **kwargs)
        sent.append(response['id'])
        raise ccxt.RequestTimeout('ack lost')
    monkeypatch.setattr(exchange, 'cancel_order', lost_ack)
    def query(record, *args, **kwargs):
        if sent:
            raise ccxt.RequestTimeout('verify unavailable')
        return original_query(record, *args, **kwargs)
    monkeypatch.setattr(service, '_query', query)
    with pytest.raises(RuntimeError, match='unresolved'):
        management.cleanup_previous_orders(service, SYMBOL)
    assert reservation_state() == 'unresolved'
    assert service._load(SYMBOL, 'LONG')['management']['pending_orders']
    before = deepcopy(exchange.writes)
    monkeypatch.setattr(service, '_query', original_query)
    assert management.recover_reservations(service, SYMBOL) == 1
    assert reservation_state() == 'finished'
    assert not service._load(SYMBOL, 'LONG')['management']['pending_orders']
    assert exchange.writes == before
    assert sent == ['old-conditional']


def test_cleanup_prewrite_read_failure_can_recover_and_resume_same_intent(managed, monkeypatch):
    service, exchange = managed
    predecessor_queue(service, exchange)
    original_query = service._query
    monkeypatch.setattr(service, '_query', lambda *args, **kwargs: (_ for _ in ()).throw(ccxt.RequestTimeout('read unavailable')))
    with pytest.raises(RuntimeError, match='unresolved'):
        management.cleanup_previous_orders(service, SYMBOL)
    assert reservation_state() == 'unresolved' and exchange.writes == []
    first_intent = service._load(SYMBOL, 'LONG')['management']['cleanup_operation']['operation_id']
    monkeypatch.setattr(service, '_query', original_query)
    assert management.recover_reservations(service, SYMBOL) == 1
    assert service._load(SYMBOL, 'LONG')['management']['pending_orders']
    management.cleanup_previous_orders(service, SYMBOL)
    finished = service._load(SYMBOL, 'LONG')['management']['cleanup_operation']
    assert finished['operation_id'] == first_intent and finished['status'] == 'completed'
    assert len(exchange.writes) == 1


def test_old_cleanup_receipt_recovers_after_successor_cycle_replacement(managed, monkeypatch):
    service, exchange = managed
    predecessor_queue(service, exchange)
    original_cancel = exchange.cancel_order
    original_query = service._query
    sent = []
    def lost_ack(*args, **kwargs):
        original_cancel(*args, **kwargs)
        sent.append(True)
        raise ccxt.RequestTimeout('ack lost')
    monkeypatch.setattr(exchange, 'cancel_order', lost_ack)
    def unavailable(record, *args, **kwargs):
        if sent:
            raise ccxt.RequestTimeout('verification unavailable')
        return original_query(record, *args, **kwargs)
    monkeypatch.setattr(service, '_query', unavailable)
    with pytest.raises(RuntimeError):
        management.cleanup_previous_orders(service, SYMBOL)
    current = service._load(SYMBOL, 'LONG')
    original_intent = deepcopy(current['management']['cleanup_operation'])
    current['episode_id'] = 'successor-after-old-cleanup'
    service._save(current)
    monkeypatch.setattr(service, '_query', original_query)
    before = deepcopy(exchange.writes)
    assert management.recover_reservations(service, SYMBOL) == 1
    latest = service._load(SYMBOL, 'LONG')
    assert latest['episode_id'] == 'successor-after-old-cleanup'
    assert latest['management']['cleanup_operation']['operation_id'] == original_intent['operation_id']
    assert latest['management']['cleanup_operation']['status'] == 'completed'
    assert exchange.writes == before


def test_cleanup_never_cancels_a_successor_identity(managed):
    service, exchange = managed
    current = predecessor_queue(service, exchange)
    current['exits'].append(deepcopy(current['management']['pending_orders'][0]))
    service._save(current)
    with pytest.raises(RuntimeError, match='unresolved'):
        management.cleanup_previous_orders(service, SYMBOL)
    assert exchange.writes == []
    assert management.recover_reservations(service, SYMBOL) == 0
    assert reservation_state() == 'unresolved'


def test_cleanup_cas_failure_preserves_concurrent_successor(managed, monkeypatch):
    service, exchange = managed
    predecessor_queue(service, exchange)
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    def concurrent_update(record, pending_plan, **kwargs):
        latest = service._load(SYMBOL, 'LONG')
        latest['management']['concurrent_marker'] = 'successor-latest'
        service._save(latest)
        raise ccxt.RequestTimeout('old cleanup worker lost response')
    monkeypatch.setattr(service, '_cancel', concurrent_update)
    with pytest.raises(ConcurrentProtectionUpdate, match='concurrent'):
        management.cleanup_previous_orders(service, SYMBOL)
    assert service._load(SYMBOL, 'LONG')['management']['concurrent_marker'] == 'successor-latest'
    assert reservation_state() == 'unresolved'


def test_triggered_predecessor_child_cannot_cancel_successor_target(managed):
    service, exchange = managed
    current = predecessor_queue(service, exchange)
    child = exchange.external_order('successor-child', role='exit', price=2565, amount=.01)
    current['exits'].append(management._external_record({**child, '_channel': 'regular'}, 'LONG', 'exit', False))
    service._save(current)
    exchange.orders['algo', 'old-conditional']['info'].update(algoStatus='TRIGGERED', actualOrderId='successor-child')
    with pytest.raises(RuntimeError, match='unresolved'):
        management.cleanup_previous_orders(service, SYMBOL)
    assert exchange.writes == []
    assert management.recover_reservations(service, SYMBOL) == 0
    assert exchange.orders['regular', 'successor-child']['status'] == 'open'


def test_background_recovery_updates_original_mcp_outer_identity_without_resubmission(managed):
    from backend.mcp import store
    _, exchange = managed
    exchange.apiKey = 'another-offline-account'
    service = make_service(exchange, owner='mcp:scope')
    management.ensure_managed(service, SYMBOL)
    principal, public_id = 'key:original-caller', 'outer-close-original'
    stable_id = 'mcp:' + store.digest(principal + ':' + public_id)
    request = {'tool': 'close', 'symbol': SYMBOL, 'arguments': {'side': 'LONG', 'amount': .01}, 'contract_version': 2}
    store.reserve_operation('scope', public_id, principal, request)
    exchange.timeout_create = True
    run_once(service.config_id, SYMBOL, stable_id, request,
             lambda: service.close(SYMBOL, 'LONG', .01, 'limit', price=2565,
                                   operation_id=stable_id, contract_version=2))
    reservation(service, stable_id)
    before = deepcopy(exchange.writes)
    assert management.recover_reservations(service, SYMBOL) == 1
    with database.get_db_conn() as conn:
        outer = conn.execute('SELECT * FROM mcp_operations WHERE profile_id=%s AND operation_id=%s', ('scope', public_id)).fetchone()
        resolved = json.loads(outer['result'])
    assert resolved['operation_id'] == public_id and outer['principal'] == principal
    assert outer['state'] in {'submitted', 'completed'}
    assert stable_id not in json.dumps(resolved)
    assert exchange.writes == before


def _callback_with_visible_ack(db_path, entered, release, crash):
    database.DB_NAME = db_path
    exchange = ExclusiveExchange()
    service = make_service(exchange)
    request = {'tool': 'close', 'args': {'side': 'LONG', 'amount': .01, 'order_type': 'limit', 'price': 2565}}
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with management.action_reservation(service, SYMBOL, 'LONG', 'visible-ack-original'):
        current = service._load(SYMBOL, 'LONG')
        order = exchange.external_order('accepted-original', amount=.01)
        record = management._external_record({**order, '_channel': 'regular'}, 'LONG', 'exit', False)
        record.update(operation_id='visible-ack-original', submission_state='submitted')
        current['exits'].append(record)
        service._save(current)
        with database.get_db_conn() as conn:
            conn.execute('INSERT INTO trade_action_runs (config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                         (service.config_id, 'visible-ack-original', SYMBOL, fingerprint, 'running',
                          '{"status":"unknown","operation_id":"visible-ack-original"}', time.time(), time.time()))
            conn.execute('INSERT INTO trade_operation_requests (config_id,operation_id,request_json) VALUES(%s,%s,%s)',
                         (service.config_id, 'visible-ack-original', encoded))
            conn.commit()
        entered.set()
        release.wait(10)
        if crash:
            os._exit(23)


@pytest.mark.parametrize('crash', [False, True])
def test_visible_ack_does_not_release_live_process_but_crash_can_recover_proof(managed, crash):
    service, exchange = managed
    exchange.external_order('accepted-original', amount=.01)
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_callback_with_visible_ack, args=(database.DB_NAME, entered, release, crash))
    process.start()
    try:
        assert entered.wait(10)
        assert reservation_state() == 'running'
        assert management.recover_reservations(service, SYMBOL) == 0
        assert reservation_state() == 'running'
        with database.get_db_conn() as conn:
            assert conn.execute('SELECT status FROM trade_action_runs WHERE operation_id=%s',
                                ('visible-ack-original',)).fetchone()[0] == 'running'
        with pytest.raises(ValueError, match='running'):
            with management.action_reservation(service, SYMBOL, 'LONG', 'new-operation'):
                pytest.fail('Visible ACK released a live callback')
        release.set()
        process.join(10)
        if crash:
            assert process.exitcode == 23
            assert management.recover_reservations(service, SYMBOL) == 1
            request = {'tool': 'close', 'args': {'side': 'LONG', 'amount': .01, 'order_type': 'limit', 'price': 2565}}
            replay = run_once(service.config_id, SYMBOL, 'visible-ack-original', request,
                              lambda: pytest.fail('Crash recovery resubmitted financial callback'))
            assert replay['status'] == 'submitted'
        else:
            assert process.exitcode == 0
        assert reservation_state() == 'finished'
        assert exchange.writes == []
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()


def test_nested_same_scope_reuses_os_lock_and_other_scope_cannot_bypass(managed):
    service, _ = managed
    with management.action_reservation(service, SYMBOL, 'LONG', 'outer-operation'):
        with management.action_reservation(service, SYMBOL, 'LONG', 'nested-same-scope'):
            assert reservation_state() == 'running'
        with pytest.raises(ValueError, match='crosses'):
            with management.action_reservation(service, SYMBOL, 'SHORT', 'nested-other-side'):
                pytest.fail('Nested action bypassed side coordination')
    assert reservation_state() == 'finished'


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
