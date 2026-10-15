"""Real PostgreSQL financial regression; never connects to a deployment URL."""
from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from backend import database, storage


def _reserve_worker(url, barrier, output):
    os.environ['DATABASE_URL'] = url
    from backend import storage
    from backend.mcp.store import reserve_operation
    storage.close_pool()
    barrier.wait(15)
    result = reserve_operation('profile', 'shared-id', 'caller', {'symbol': 'BTC/USDT'})
    output.put(result)
    storage.close_pool()


def test_same_operation_is_reserved_once_across_processes(financial_pg):
    context = multiprocessing.get_context('spawn')
    barrier, output = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_reserve_worker, args=(financial_pg, barrier, output)) for _ in range(2)]
    for worker in workers:
        worker.start()
    results = [output.get(timeout=25) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert sum(result is None for result in results) == 1
    assert next(result for result in results if result is not None)['status'] == 'unknown'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM mcp_operations').fetchone()[0] == 1


def test_scope_lock_does_not_block_another_account(financial_pg):
    with database.get_db_conn() as first:
        storage.lock_scope(first, 'paper-account', 'namespace', 'one')
        with storage.get_connection(timeout=.1) as other:
            storage.lock_scope(other, 'paper-account', 'namespace', 'two')
            other.commit()
        with pytest.raises(storage.OperationalError):
            with storage.get_connection(timeout=.1) as competing:
                storage.lock_scope(competing, 'paper-account', 'namespace', 'one')


def test_paper_money_and_receipts_are_atomic_under_competition(financial_pg):
    from backend.mcp import paper, store
    store.put('paper_settings', 'global', {'initial_balance': 1000, 'loan_limit': 500})
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'unit', 'profile_ids': [], 'scopes': ['read', 'trade']}
    paper.create_account(caller, 'user', 'create-user')
    def borrow(operation):
        try:
            return paper.bank(caller, 'user', 'borrow', 400, operation)
        except ValueError as error:
            return error
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(borrow, ['borrow-one', 'borrow-two']))
    assert sum(isinstance(result, dict) for result in results) == 1
    account = paper.get_account(caller, 'user')
    assert account['cash'] == 1400 and account['debt'] == 400
    accepted = next(result for result in results if isinstance(result, dict))
    assert paper.bank(caller, 'user', 'borrow', 400, accepted['operation_id']) == accepted
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM paper_operations').fetchone()[0] == 2


def test_paper_ordered_batch_keeps_parent_child_identities(financial_pg, monkeypatch):
    from backend.mcp import paper, paper_batch, store
    import time
    store.put('paper_settings', 'global', {'initial_balance': 1000, 'loan_limit': 500, 'fee_rate': .001})
    monkeypatch.setattr(paper, 'market', lambda symbol, market_type='swap', *a, **k: {
        'symbol': symbol, 'market_type': market_type, 'price': 100, 'bid': 100, 'ask': 100,
        'exchange': 'binance', 'observed_at': time.time()})
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'unit', 'profile_ids': [], 'scopes': ['read', 'trade']}
    paper.create_account(caller, 'user', 'create-user')
    request = {'contract_version': 3, 'actions': [{'action': 'open', 'market_type': 'spot',
        'symbol': 'BTC/USDT', 'side': 'LONG', 'order_type': 'market', 'amount': 1, 'reason': 'requested'}]}
    result = paper_batch.execute(caller, 'user', request, 'parent-one')
    assert result['status'] == 'completed', result
    assert result['results'][0]['operation_id'] != result['operation_id']
    assert paper_batch.execute(caller, 'user', request, 'parent-one') == result
    assert paper.get_account(caller, 'user')['spot'][0]['amount'] == 1


def test_spot_reservations_do_not_overspend(financial_pg):
    from backend.utils.spot_execution import current_spot_cycle_id, ensure_spot_budget_cycle, reserve_spot_batch
    config = {'dca_amount': 100, 'dca_budget': 1000}
    ensure_spot_budget_cycle('cfg', config, 'cycle')
    def reserve(identity):
        token = current_spot_cycle_id.set('cycle')
        try:
            reserve_spot_batch('cfg', config, identity, [SimpleNamespace(symbol='BTC/USDT', entry_price=80, amount=1)])
            return True
        except ValueError:
            return False
        finally:
            current_spot_cycle_id.reset(token)
    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(reserve, ['one', 'two'])) == 1
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT sum(quote_cost) FROM spot_budget_reservations').fetchone()[0] == 80


@pytest.mark.parametrize('failure_mode', ['checkout', 'disconnect'])
def test_ack_then_database_failure_never_replays_callback(financial_pg, monkeypatch, failure_mode):
    from backend.utils.trade_operations import run_once
    original = database.get_db_conn
    executions = []
    @contextmanager
    def broken():
        if failure_mode == 'checkout':
            raise storage.OperationalError('Injected commit channel unavailable')
        with original() as conn:
            pid = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
            with psycopg.connect(financial_pg, autocommit=True) as killer:
                assert killer.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
            yield conn
    def exchange_ack():
        executions.append('exchange-accepted-original-id')
        monkeypatch.setattr(database, 'get_db_conn', broken)
        return {'status': 'submitted', 'operation_id': 'same-id', 'order_id': 'exchange-one'}
    receipt = run_once('cfg', 'BTC/USDT', 'same-id', {'contract_version': 3, 'action': 'open'}, exchange_ack)
    assert receipt['status'] == 'submitted' and receipt['persistence_pending']
    monkeypatch.setattr(database, 'get_db_conn', original)
    replay = run_once('cfg', 'BTC/USDT', 'same-id', {'contract_version': 3, 'action': 'open'}, exchange_ack)
    assert replay['status'] == 'unknown'
    assert executions == ['exchange-accepted-original-id']


def test_byte_pages_preserve_utf8_and_raw_payload_limits(financial_pg, monkeypatch):
    from backend.utils.sqlite_blob import bounded_text, text_page
    from backend.mcp.paper_storage import raw_audit_chunk
    from backend.utils.runtime_capacity import CapacityBusy
    raw = '中文🙂abc' * 20
    with database.get_db_conn() as conn:
        identity = conn.execute('INSERT INTO paper_accounts(namespace,user_id,payload) VALUES(%s,%s,%s) RETURNING storage_id',
                                ('ns', 'user', raw)).fetchone()[0]
        conn.commit()
        offset, parts = 0, []
        while True:
            page = text_page(conn, 'paper_accounts', 'payload', identity, offset=offset, length=3)
            parts.append(page['text'])
            if not page['has_more']:
                break
            offset = page['next_offset']
        assert ''.join(parts) == raw
        with pytest.raises(UnicodeDecodeError):
            text_page(conn, 'paper_accounts', 'payload', identity, offset=1, length=3)
        monkeypatch.setenv('RUNTIME_CAPACITY_LIMITS_ENABLED', 'true')
        with pytest.raises(CapacityBusy):
            with bounded_text(conn, 'paper_accounts', 'payload', identity, limit=3):
                pass
        conn.rollback()
        first = raw_audit_chunk(conn, 'ns', 'user', 'account', length=2, encoding='text')
        assert first['text'] == '中文' and first['next_offset'] == 6


def _plan(conn, config='cfg', symbol='BTC/USDT:USDT', **extra):
    payload = {'account_scope': 'scope', 'symbol': symbol, 'side': 'LONG', 'state': 'OPEN', 'updated_at': 1, **extra}
    return conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s) RETURNING storage_id',
                        (config, symbol, 'LONG', json.dumps(payload))).fetchone()[0]


def test_projection_and_revision_rollback_with_source(financial_pg):
    with database.get_db_conn() as conn:
        identity = _plan(conn)
        assert conn.execute('SELECT state FROM protection_plan_activity WHERE plan_rowid=%s', (identity,)).fetchone()[0] == 'OPEN'
        assert conn.execute('SELECT revision FROM plan_source_revisions').fetchone()[0] == 1
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM protection_plan_activity').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM plan_source_revisions').fetchone()[0] == 0


def test_late_identity_commit_resets_persistent_index(financial_pg):
    from backend.utils.execution_plan_index import advance_index
    ledger = SimpleNamespace(scope='scope', symbol='BTC/USDT:USDT', config_id='cfg')
    ledger._retire_legacy_algo_aliases = lambda *a, **k: (None, None, [])
    seen = []
    ledger._index_plan = lambda owner, plan: seen.append(plan['marker']) or False
    ledger._index_legacy_row = lambda *a: False
    with database.get_db_conn() as late:
        # Allocate low identity before another transaction commits higher ID.
        low = late.execute("SELECT nextval(pg_get_serial_sequence('real_protection_plans','storage_id'))").fetchone()[0]
        with database.get_db_conn() as early:
            high = _plan(early, config='cfg-high', marker='high')
            early.commit()
        for _ in range(30):
            if advance_index(ledger)['complete']:
                break
        assert seen == ['high'] and high > low
        value = {'account_scope': 'scope', 'symbol': ledger.symbol, 'side': 'LONG', 'state': 'OPEN', 'marker': 'late'}
        late.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload,storage_id) VALUES(%s,%s,%s,%s,%s)',
                     ('cfg-low', ledger.symbol, 'LONG', json.dumps(value), low))
        late.commit()
    for _ in range(30):
        if advance_index(ledger)['complete']:
            break
    assert 'late' in seen
    with database.get_db_conn() as conn:
        conn.execute('UPDATE real_protection_plans SET payload=%s WHERE storage_id=%s',
                     (json.dumps({**value, 'marker': 'updated'}), low))
        conn.commit()
    for _ in range(30):
        if advance_index(ledger)['complete']:
            break
    assert 'updated' in seen
    seen.clear()
    with database.get_db_conn() as conn:
        conn.execute('DELETE FROM real_protection_plans WHERE storage_id=%s', (low,))
        conn.commit()
    for _ in range(30):
        if advance_index(ledger)['complete']:
            break
    assert seen == ['high']


def test_source_change_during_history_rebuild_retains_previous(financial_pg, monkeypatch):
    from backend.utils import execution_ledger
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_position_history(position_id,account_scope,config_id,symbol,payload) '
                     'VALUES(%s,%s,%s,%s,%s)', ('previous', 'scope', 'cfg', 'BTC/USDT:USDT', '{"complete":true}'))
        conn.commit()
    def reconstruction(*args, **kwargs):
        with database.get_db_conn() as writer:
            writer.execute('INSERT INTO execution_order_links(account_scope,symbol,order_id,config_id,role,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                            ('scope', 'BTC/USDT:USDT', 'late-order', 'cfg', 'entry', '{}'))
            writer.commit()
        return {'scan_complete': True}
    monkeypatch.setattr(execution_ledger, '_reconstruct_position_cycles', reconstruction)
    result = execution_ledger.rebuild_execution_position_history('cfg', 'scope', 'BTC/USDT:USDT')
    assert not result['published'] and result['retained_previous']
    assert result['reason_code'] == 'cycle_source_changed'
    with database.get_db_conn() as conn:
        previous = conn.execute('SELECT position_id,payload FROM execution_position_history').fetchone()
        assert tuple(previous) == ('previous', '{"complete":true}')


def test_unrelated_scope_does_not_invalidate_history(financial_pg, monkeypatch):
    from backend.utils import execution_ledger
    def reconstruction(*args, **kwargs):
        with database.get_db_conn() as writer:
            writer.execute('INSERT INTO execution_order_links(account_scope,symbol,order_id,config_id,role,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                            ('other-scope', 'BTC/USDT:USDT', 'other-order', 'cfg', 'entry', '{}'))
            writer.commit()
        return {'scan_complete': True}
    monkeypatch.setattr(execution_ledger, '_reconstruct_position_cycles', reconstruction)
    assert execution_ledger.rebuild_execution_position_history('cfg', 'scope', 'BTC/USDT:USDT')['published']


def test_native_financial_statements_prepare_against_postgres(financial_pg):
    """Catch invalid native SQL even in rarely reached financial branches."""
    count = 0
    with database.get_db_conn() as conn:
        conn.execute('CREATE TEMP TABLE execution_history_stage AS SELECT * FROM execution_position_history WHERE false')
        for root in ('backend/utils', 'backend/mcp', 'backend/agent'):
            for path in Path(root).glob('*.py'):
                if path.name == 'chat_graph.py':
                    continue
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in {'execute', 'executemany'} and node.args
                            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                        continue
                    query = node.args[0].value
                    if not re.match(r'\s*(SELECT|UPDATE|INSERT|DELETE)\b', query, re.I):
                        continue
                    positions = iter(range(1, 200))
                    query = re.sub('%[sbt]', lambda _: '$' + str(next(positions)), query).replace('%%', '%')
                    try:
                        conn.execute('PREPARE financial_check AS ' + query)
                        conn.execute('DEALLOCATE financial_check')
                    except psycopg.Error as exc:
                        pytest.fail(f'{path}:{node.lineno}: {exc}')
                    count += 1
        assert count > 500


def test_deployment_pause_rejects_financial_tools_before_intent(financial_pg, monkeypatch):
    from backend.mcp import paper, paper_batch
    from backend.utils.trade_operations import run_once
    from backend.agent.agent_tools import open_position_strategy
    monkeypatch.setenv('DEPLOYMENT_MAINTENANCE', 'true')
    caller = {'id': 'key:paper', 'key_type': 'paper', 'paper_namespace': 'unit', 'profile_ids': [], 'scopes': ['read', 'trade']}
    with pytest.raises(ValueError, match='deployment_maintenance'):
        run_once('cfg', 'BTC/USDT', 'paused-id', {}, lambda: pytest.fail('No exchange call permitted'))
    with pytest.raises(ValueError, match='deployment_maintenance'):
        paper.create_account(caller, 'user', 'create-user')
    with pytest.raises(ValueError, match='deployment_maintenance'):
        open_position_strategy.func([], 'cfg', 'BTC/USDT')
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM trade_action_runs').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM paper_accounts').fetchone()[0] == 0


def test_bounded_read_detects_size_change_between_metadata_and_slice(financial_pg, monkeypatch):
    from backend.utils import sqlite_blob
    with database.get_db_conn() as conn:
        identity = conn.execute('INSERT INTO paper_accounts(namespace,user_id,payload) VALUES(%s,%s,%s) RETURNING storage_id',
                                ('ns', 'user', 'before')).fetchone()[0]
        conn.commit()
        original = sqlite_blob.blob_slice
        def concurrent_update(*args, **kwargs):
            with database.get_db_conn() as writer:
                writer.execute('UPDATE paper_accounts SET payload=%s WHERE storage_id=%s', ('larger-value', identity))
                writer.commit()
            return original(*args, **kwargs)
        monkeypatch.setattr(sqlite_blob, 'blob_slice', concurrent_update)
        with pytest.raises(ValueError, match='changed during bounded read'):
            with sqlite_blob.bounded_text(conn, 'paper_accounts', 'payload', identity, limit=100):
                pytest.fail('A truncated changed JSON value must not escape')


def test_plan_evidence_rejects_concurrent_stale_version(financial_pg):
    from backend.utils.trade_evidence import save_plan
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    plan = {'account_scope': 'scope', 'symbol': 'BTC/USDT:USDT', 'episode_id': 'episode', 'record_version': 0}
    with database.get_db_conn() as conn:
        save_plan(conn, 'cfg', plan)
        conn.commit()
    def update(_):
        try:
            with database.get_db_conn() as conn:
                save_plan(conn, 'cfg', {**plan, 'record_version': 1}, expected_version=0)
                conn.commit()
            return True
        except ConcurrentProtectionUpdate:
            return False
    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(update, range(2))) == 1


def test_memory_source_symbol_filter_is_native_postgres(financial_pg, monkeypatch):
    from backend.agent.context_projections import current_sources
    from backend.config import config
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'symbol': 'BTC/USDT', 'mode': 'STRATEGY'})
    with database.get_db_conn() as conn:
        for index, symbol in enumerate(('BTC/USDT:USDT', 'ETH/USDT:USDT')):
            conn.execute('INSERT INTO short_memories(config_id,symbol,bucket_start,bucket_end,market_summary,source_summary_ids) VALUES(%s,%s,%s,%s,%s,%s)',
                         ('cfg', symbol, f'2026-10-15 0{index}:00:00', f'2026-10-15 0{index + 4}:00:00', symbol, '[]'))
        conn.commit()
    sources = current_sources('cfg', kind='short_memory')
    assert len(sources) == 1 and sources[0]['text'] == 'BTC/USDT:USDT'
    assert 'storage_id' not in sources[0]['row']


def test_public_audit_and_management_rows_hide_internal_scan_identity(financial_pg):
    from backend.mcp import store
    from backend.utils.visible_management import _decode
    store.audit('caller', 'query', 'completed')
    assert 'storage_id' not in store.audit_rows()[0]
    with database.get_db_conn() as conn:
        raw = conn.execute("SELECT 7::bigint AS storage_id,'{}'::text AS payload,'claim'::text AS claim_id").fetchone()
        assert _decode(raw) == {'claim_id': 'claim'}


def test_unified_account_snapshot_hides_internal_scan_identity(financial_pg):
    from backend.agent.unified_dispatch import live_account_summary
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO mock_accounts(config_id,symbol,balance) VALUES('cfg','BTC/USDT',1000)")
        conn.execute("INSERT INTO mock_orders(order_id,config_id,symbol,is_filled,status) VALUES('order','cfg','BTC/USDT',1,'OPEN')")
        conn.commit()
    parsed = SimpleNamespace(actions=[SimpleNamespace(symbol='BTC/USDT')])
    view = live_account_summary('cfg', parsed, {'mode': 'STRATEGY'})
    assert view['status'] == 'completed' and len(view['positions']) == 1
    assert 'storage_id' not in view['positions'][0]


def test_history_disconnect_propagates_and_retains_previous(financial_pg, monkeypatch):
    from backend.utils import execution_ledger
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_position_history(position_id,account_scope,config_id,symbol,payload) '
                     'VALUES(%s,%s,%s,%s,%s)', ('previous', 'scope', 'cfg', 'BTC/USDT:USDT', '{"complete":true}'))
        conn.commit()

    def disconnected(*args, read_conn, **kwargs):
        # Terminate only this test's known scan session, never the shared server.
        pid = read_conn.execute('SELECT pg_backend_pid()').fetchone()[0]
        with psycopg.connect(financial_pg, autocommit=True) as killer:
            assert killer.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0]
        return {'scan_complete': True}

    monkeypatch.setattr(execution_ledger, '_reconstruct_position_cycles', disconnected)
    with pytest.raises(storage.OperationalError):
        execution_ledger.rebuild_execution_position_history('cfg', 'scope', 'BTC/USDT:USDT')
    with database.get_db_conn() as conn:
        assert tuple(conn.execute('SELECT position_id,payload FROM execution_position_history').fetchone()) == (
            'previous', '{"complete":true}')


def test_ownership_index_keeps_pending_owner_when_history_cannot_publish(financial_pg, monkeypatch):
    from backend.utils import execution_ledger, execution_plan_index
    ledger = SimpleNamespace(scope='scope', symbol='BTC/USDT:USDT', config_id='cfg')
    index_id = hashlib.sha256(json.dumps([ledger.scope, ledger.symbol, ledger.config_id]).encode()).hexdigest()
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_plan_index_runs(index_id,account_scope,symbol,config_id,phase,cursor,source_revision) '
                     'VALUES(%s,%s,%s,%s,%s,0,0)', (index_id, ledger.scope, ledger.symbol, ledger.config_id, 'history'))
        conn.execute('INSERT INTO execution_plan_index_affected(index_id,owner) VALUES(%s,%s)', (index_id, 'cfg'))
        conn.commit()
    monkeypatch.setattr(execution_ledger, 'rebuild_execution_position_history', lambda *args: {
        'scan_complete': False, 'published': False, 'reason_code': 'cycle_source_changed', 'retained_previous': True})
    result = execution_plan_index.advance_index(ledger)
    assert not result['complete'] and result['phase'] == 'history'
    assert result['error'] == 'cycle_source_changed'
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT owner FROM execution_plan_index_affected WHERE index_id=%s', (index_id,)).fetchone()[0] == 'cfg'
        assert conn.execute('SELECT phase FROM execution_plan_index_runs WHERE index_id=%s', (index_id,)).fetchone()[0] == 'history'
