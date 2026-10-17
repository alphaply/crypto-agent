"""Isolated PostgreSQL regression for the 2026-10-17 history backlog."""
import hashlib
import json
import time
import os
import subprocess
import sys
from threading import Event, Thread

import pytest
import psycopg

from backend import database
from backend.utils.execution_ledger import ExecutionLedger
from backend.utils import execution_plan_index as index

SYMBOL = 'ETH/USDT:USDT'


class Exchange:
    id, apiKey = 'binance', 'latency-fixture'
    markets = {SYMBOL: dict(symbol=SYMBOL, contract=True, swap=True, linear=True)}

    def market(self, _):
        return self.markets[SYMBOL]


def drain(ledger, maximum=500):
    for _ in range(maximum):
        state = ledger.index_saved_plans()
        if state['complete']:
            return state
        assert not state.get('error'), state
    pytest.fail('Index did not converge')


def event(conn, payload, *, source_id=None):
    if source_id is None:
        return conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                            'VALUES(%s,%s,%s,%s) RETURNING id', ('2026-10-17 15:00:00','cfg',SYMBOL,json.dumps(payload))).fetchone()[0]
    conn.execute('INSERT INTO real_protection_events(id,timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s,%s)',
                 (source_id,'2026-10-17 15:00:00','cfg',SYMBOL,json.dumps(payload)))


@pytest.fixture
def history(financial_pg):
    return ExecutionLedger(Exchange(), 'cfg', SYMBOL)


def test_constructor_does_no_historical_work(history, monkeypatch):
    with database.get_db_conn() as conn:
        event(conn, dict(entries=[dict(id='1')]))
        conn.commit()
    monkeypatch.setattr(index, 'advance_index', lambda _: pytest.fail('Constructor scanned history'))
    ledger = ExecutionLedger(Exchange(), 'cfg', SYMBOL)
    assert not ledger.index_state['complete']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_plan_index_runs').fetchone()[0] == 0


def test_slices_deduplicate_and_preserve_cross_page_regular_evidence(history, monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    with database.get_db_conn() as conn:
        event(conn, dict(execution_mode='independent_exits',exits=[dict(id='7',order_type='stop_market')]))
        for _ in range(5):
            event(conn, dict(entries=[]))
        event(conn, dict(entries=[dict(id='7')]))
        conn.commit()
    state = history.index_saved_plans()
    assert not state['complete'] and state['processed'] <= 2
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_order_links').fetchone()[0] == 0
    drain(history)
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT role FROM execution_order_links WHERE order_id='7'").fetchone()[0] == 'entry'
        assert conn.execute('SELECT count(*) FROM ownership_projections').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM real_protection_events').fetchone()[0] == 7
    assert history.index_saved_plans()['processed'] == 0


def test_late_smaller_identity_update_delete_and_symbol_move(history):
    with database.get_db_conn() as conn:
        lower = conn.execute("SELECT nextval(pg_get_serial_sequence('real_protection_events','id'))").fetchone()[0]
        high = event(conn, dict(entries=[dict(id='high')]))
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        event(conn, dict(entries=[dict(id='late')]),source_id=lower)
        conn.execute('UPDATE real_protection_events SET payload=%s WHERE id=%s',
                     (json.dumps(dict(entries=[dict(id='corrected')])), high))
        conn.commit()
    drain(ExecutionLedger(Exchange(),'cfg',SYMBOL))
    with database.get_db_conn() as conn:
        facts = {r[0] for r in conn.execute('SELECT order_id FROM execution_plan_index_facts')}
        assert 'late' in facts and 'corrected' in facts and 'high' not in facts
        conn.execute('DELETE FROM real_protection_events WHERE id=%s',(high,))
        conn.execute('UPDATE real_protection_events SET symbol=%s WHERE id=%s',('ETH/USDT',lower))
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        facts = {r[0] for r in conn.execute('SELECT order_id FROM execution_plan_index_facts')}
        assert facts == {'late'}


def test_history_work_does_not_take_financial_account_lock(history):
    from backend.utils.position_protection import _lock
    entered, release = Event(), Event()
    lock = _lock(history.scope)
    def holder():
        with lock:
            entered.set()
            assert release.wait(10)
    worker = Thread(target=holder)
    worker.start()
    try:
        assert entered.wait(3)
        # Index completes before the financial lock is released.
        assert history.index_saved_plans()['complete']
    finally:
        release.set()
        worker.join(5)


def test_5147_snapshots_are_compact_and_incremental(history, monkeypatch, record_property):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE','128')
    monkeypatch.setenv('RUNTIME_INDEX_SCAN_SECONDS','.2')
    payload = json.dumps(dict(entries=[dict(id='entry',price=2460)], notes='x'*13000))
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                     "SELECT '2026-10-17 15:00:00','cfg',%s,%s FROM generate_series(1,5147)", (SYMBOL,payload))
        conn.commit()
    calls = [0]
    execute = psycopg.Connection.execute
    def counted(self,*args,**kwargs):
        calls[0] += 1
        return execute(self,*args,**kwargs)
    monkeypatch.setattr(psycopg.Connection,'execute',counted)
    from backend.utils.resource_diagnostics import process_rss_bytes
    durations, rss = [], []
    advance = history.index_saved_plans
    def measured():
        start = time.monotonic()
        state = advance()
        durations.append(time.monotonic()-start)
        value = process_rss_bytes()
        if value is not None:
            rss.append(value)
        return state
    monkeypatch.setattr(history,'index_saved_plans',measured)
    started = time.monotonic()
    first = history.index_saved_plans()
    assert first['processed'] <= 128 and not first['complete']
    drain(history)
    initial_calls = calls[0]
    record_property('initial_sql_calls', initial_calls)
    record_property('initial_seconds', time.monotonic()-started)
    record_property('max_slice_seconds',max(durations))
    if rss:
        record_property('sampled_python_rss_max_bytes',max(rss))
    assert max(durations) < 2
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM ownership_projections').fetchone()[0] == 1
        assert conn.execute('SELECT sum(octet_length(payload)) FROM real_protection_events').fetchone()[0] > 67_000_000
        assert conn.execute('SELECT sum(octet_length(payload)) FROM ownership_sources').fetchone()[0] < 1_000_000
        event(conn, dict(entries=[dict(id='next',price=2500)]))
        conn.commit()
    before = calls[0]
    state = drain(history)
    record_property('incremental_sql_calls', calls[0]-before)
    assert calls[0]-before < 100
    before = calls[0]
    assert history.index_saved_plans()['processed'] == 0
    assert calls[0]-before < 15


def test_source_version_is_not_cleared_by_stale_worker(history):
    with database.get_db_conn() as conn:
        ident = event(conn, dict(entries=[dict(id='first')]))
        conn.commit()
        stale = index._pending(conn, history, index.index_id(history),1)[0]
    with database.get_db_conn() as conn:
        conn.execute('UPDATE real_protection_events SET payload=%s WHERE id=%s',
                     (json.dumps(dict(entries=[dict(id='newer')])),ident))
        conn.commit()
    history.index_saved_plans()
    with database.get_db_conn() as conn:
        index._collect(conn,history,index.index_id(history),stale)
        conn.commit()
    # A stale source version stays discoverably pending rather than deleting a new event.
    drain(history)
    with database.get_db_conn() as conn:
        assert {r[0] for r in conn.execute('SELECT order_id FROM execution_plan_index_facts')} == {'newer'}


def test_frontend_action_returns_while_real_history_worker_is_blocked(financial_pg, monkeypatch, record_property):
    from backend.agent import agent_tools, tool_registry
    from backend.config import config
    from test_single_call_execution import AckThenReadsFail
    from test_exclusive_position_management import make_service
    venue = AckThenReadsFail()
    executor = make_service(venue,'offline-task',side='SHORT',policy='visible_first_claim')
    cfg = {**executor.mt.runtime_config,'enabled':True}
    executor.mt.runtime_config = cfg
    monkeypatch.setattr(config,'get_config_by_id',lambda _:cfg)
    monkeypatch.setattr(agent_tools,'MarketTool',lambda **_:executor.mt)
    history = ExecutionLedger(venue,'offline-task',SYMBOL)
    with database.get_db_conn() as conn:
        payload = json.dumps(dict(account_scope=history.scope, side='SHORT',entries=[dict(id='old')], notes='x'*13000))
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                     "SELECT '2026-10-17 15:00:00','offline-task',%s,%s FROM generate_series(1,5147)", (SYMBOL,payload))
        conn.commit()
    entered, release = Event(), Event()
    errors = []
    original = index._collect
    def blocked(*args):
        entered.set()
        assert release.wait(15)
        return original(*args)
    monkeypatch.setattr(index,'_collect',blocked)
    monkeypatch.setattr('backend.utils.execution_ledger.rebuild_execution_position_history',
                        lambda *_: pytest.fail('Foreground rebuilt historical PnL'))
    def work():
        try:
            history.index_saved_plans()
        except Exception as exc:
            errors.append(exc)
    worker = Thread(target=work)
    worker.start()
    try:
        assert entered.wait(5)
        assert history.index_saved_plans()['phase'] == 'busy'
        started = time.monotonic()
        args = dict(contract_version=3,actions=[dict(action='open',side='SHORT',amount=.05,price=2460,reason='latency regression')])
        result = json.loads(tool_registry.run_trade_tool('execute_trade_actions',args,'offline-task',SYMBOL,'latency-open'))
        elapsed = time.monotonic()-started
        record_property('foreground_seconds_with_blocked_67mb_history',elapsed)
        assert elapsed < 5
        assert not release.is_set()
        assert result['status'] == 'submitted',result
        assert len(venue.writes) == 1
        repeated = json.loads(tool_registry.run_trade_tool('execute_trade_actions',args,'offline-task',SYMBOL,'latency-open'))
        assert repeated['status'] == 'submitted'
        assert len(venue.writes) == 1
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors


def test_timing_records_stages_without_payloads(caplog):
    from backend.utils.execution_timing import stage, logger
    import logging
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO,logger='ExecutionTiming'):
            with stage('exchange_submit'):
                pass
            with pytest.raises(ValueError):
                with stage('ack_persist'):
                    raise ValueError('private payload must not be logged')
    finally:
        logger.removeHandler(caplog.handler)
    assert 'stage=exchange_submit result=completed' in caplog.text
    assert 'stage=ack_persist result=failed' in caplog.text
    assert 'private payload' not in caplog.text


def test_index_work_lease_is_shared_across_processes(history):
    from backend.utils.management_lock import scope_file_lock
    script = '''import json
from backend.utils.execution_ledger import ExecutionLedger
class Exchange:
    id,apiKey='binance','latency-fixture'
    markets={'ETH/USDT:USDT':dict(symbol='ETH/USDT:USDT',contract=True,swap=True,linear=True)}
    def market(self,_): return self.markets['ETH/USDT:USDT']
print(json.dumps(ExecutionLedger(Exchange(),'cfg','ETH/USDT:USDT').index_saved_plans()))
'''
    with scope_file_lock('ownership-index',history.scope,SYMBOL+'\0cfg'):
        child = subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,
                               env=os.environ.copy(),timeout=10)
    assert child.returncode == 0
    assert json.loads(child.stdout.splitlines()[-1])['phase'] == 'busy'


def test_history_queue_preserves_newer_request_during_publication(history, monkeypatch):
    from backend.utils.execution_history_work import request_history,drain_history
    request_history('cfg',history.scope,SYMBOL)
    def rebuild(*_):
        request_history('cfg',history.scope,SYMBOL)
        return {'published':True}
    monkeypatch.setattr('backend.utils.execution_ledger.rebuild_execution_position_history',rebuild)
    drain_history('cfg',history.scope,SYMBOL)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_history_work').fetchone()[0] == 1


def test_observation_only_update_does_not_invalidate_completed_index(history):
    raw = dict(account_scope=history.scope,side='LONG',entries=[dict(id='unchanged')],updated_at=1)
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                     ('cfg',SYMBOL,'LONG',json.dumps(raw)))
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        raw.update(updated_at=2,position_observation={'amount':3,'status':'verified'})
        conn.execute('UPDATE real_protection_plans SET payload=%s',(json.dumps(raw),))
        conn.commit()
    assert history.index_saved_plans()['processed'] == 0


def test_real_legacy_source_retired_when_mode_becomes_null(history):
    with database.get_db_conn() as conn:
        ident = conn.execute("INSERT INTO orders(config_id,symbol,trade_mode,side,order_id) "
                             "VALUES('cfg',%s,'REAL','BUY','legacy') RETURNING id", (SYMBOL,)).fetchone()[0]
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        conn.execute('UPDATE orders SET trade_mode=NULL WHERE id=%s',(ident,))
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_plan_index_facts').fetchone()[0] == 0


def test_current_baseline_does_not_require_history_before_background_observation(financial_pg):
    from backend.utils.trade_operations import financial_ack_only
    from backend.utils.position_management import ensure_managed
    from test_exclusive_position_management import ExclusiveExchange,make_service
    executor = make_service(ExclusiveExchange(),policy='visible_first_claim')
    token = financial_ack_only.set(True)
    try:
        ensure_managed(executor,SYMBOL,'LONG')
    finally:
        financial_ack_only.reset(token)
    assert executor._load(SYMBOL,'LONG')['management']['baseline_contracts'] == .02
    ensure_managed(executor,SYMBOL,'LONG')
    assert executor._load(SYMBOL,'LONG')['management']['contracts'] == .02
    assert not executor.ex.writes


def test_pending_projection_reads_are_byte_bounded(history):
    entries = [dict(id='x'*40+str(i)) for i in range(20000)]
    with database.get_db_conn() as conn:
        for _ in range(5):
            event(conn,dict(entries=entries))
        conn.commit()
        rows = index._pending(conn,history,index.index_id(history),128)
    assert 0 < len(rows) < 5
    assert sum(len((row['payload'] or '').encode()) for row in rows) <= 4*1024*1024


def test_exit_order_projection_preserves_object_and_optional_null(history):
    with database.get_db_conn() as conn:
        ident = event(conn,dict(exit_order=dict(id='exit-object',price=2400),side='LONG'))
        conn.commit()
    drain(history)
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT role FROM execution_order_links WHERE order_id='exit-object'").fetchone()[0] == 'emergency_exit'
        conn.execute('UPDATE real_protection_events SET payload=%s WHERE id=%s',
                     (json.dumps(dict(exit_order=None)),ident))
        conn.commit()
    assert drain(history)['complete']


def test_large_projection_publishes_one_order_per_work_unit(history, monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE','2')
    with database.get_db_conn() as conn:
        event(conn,dict(entries=[dict(id=str(i)) for i in range(20)]))
        conn.commit()
    state = history.index_saved_plans()
    assert not state['complete'] and state['processed'] <= 2
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_order_links').fetchone()[0] <= 1
    drain(history)
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_order_links').fetchone()[0] == 20
