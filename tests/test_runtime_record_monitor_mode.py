"""Monitor-only resource sizing preserves complete original financial evidence."""
from datetime import datetime
import json
import sqlite3

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils import protection_scan, runtime_capacity
from backend.utils.execution_ledger import (ExecutionLedger, register_order,
                                            rebuild_execution_position_history)
from backend.utils.position_protection import PositionProtection
from backend.utils.trade_recovery import latest_operation_receipts
from test_execution_ledger import Exchange, fill


def _mode(monkeypatch, enabled):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', str(enabled).lower())
    monkeypatch.setattr(runtime_capacity, '_manager', runtime_capacity.CapacityManager.from_environment())


@pytest.fixture
def record_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'records.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        conn.commit()


@pytest.mark.parametrize('enabled', [True, False])
def test_receipt_size_gate_bypass_preserves_task_symbol_and_row_limit(record_db, monkeypatch, enabled):
    _mode(monkeypatch, enabled)
    monkeypatch.setenv('RUNTIME_FINANCIAL_PAYLOAD_BYTES', '100')
    receipt = {'receipt_version': 1, 'status': 'unknown', 'original_evidence': 'x' * 2000}
    raw = json.dumps(receipt)
    with database.get_db_conn() as conn:
        for config_id, identity, symbol, updated in (
                ('owner', 'original', 'ETH/USDT', 2), ('owner', 'other-symbol', 'BTC/USDT', 1),
                ('foreign', 'foreign-operation', 'ETH/USDT', 3)):
            conn.execute('INSERT INTO trade_action_runs '
                         '(config_id,operation_id,symbol,request_hash,status,result,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (config_id, identity, symbol, 'fixture-hash', 'unknown', raw, 1, updated))
        conn.commit()
    result = latest_operation_receipts('owner', 'ETH/USDT', limit=1)
    if enabled:
        assert result == {}
    else:
        assert set(result) == {'original'}
        assert result['original']['original_evidence'] == receipt['original_evidence']
        assert result['original']['status'] == 'unknown'
    with database.get_db_conn() as conn:
        assert [row[0] for row in conn.execute('SELECT result FROM trade_action_runs')] == [raw] * 3


def test_activity_projection_mode_transition_rescans_once_in_original_bounded_pages(monkeypatch):
    _mode(monkeypatch, True)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE real_protection_plans(config_id TEXT,symbol TEXT,side TEXT,payload TEXT)')
    raw = json.dumps({'state': 'DONE', 'updated_at': 123, 'original_evidence': 'x' * 2000})
    for index in range(5):
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', (str(index), str(index), 'LONG', raw))
    invalid = 'not valid json' * 100
    conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('invalid', 'bad', 'LONG', invalid))
    protection_scan.initialize_schema(conn.cursor())
    while not protection_scan.migrate_activity_page(conn.cursor()):
        pass
    assert {row[0] for row in conn.execute('SELECT state FROM protection_plan_activity')} == {'UNKNOWN'}

    _mode(monkeypatch, False)
    protection_scan.initialize_schema(conn.cursor())
    progress = conn.execute('SELECT cursor,complete FROM protection_plan_scan_progress').fetchone()
    assert tuple(progress) == (2, 0)
    assert conn.execute("SELECT count(*) FROM protection_plan_activity WHERE state='DONE'").fetchone()[0] == 2
    protection_scan.initialize_schema(conn.cursor())
    assert conn.execute('SELECT cursor FROM protection_plan_scan_progress').fetchone()[0] == 4
    while not protection_scan.migrate_activity_page(conn.cursor()):
        pass
    final_progress = tuple(conn.execute('SELECT cursor,complete FROM protection_plan_scan_progress').fetchone())
    protection_scan.initialize_schema(conn.cursor())
    assert tuple(conn.execute('SELECT cursor,complete FROM protection_plan_scan_progress').fetchone()) == final_progress
    assert conn.execute("SELECT count(*) FROM protection_plan_activity WHERE state='DONE'").fetchone()[0] == 5
    assert [row['config_id'] for row in protection_scan.active_plan_page(conn, limit=2)] == ['invalid']
    assert [row[0] for row in conn.execute('SELECT payload FROM real_protection_plans')] == [raw] * 5 + [invalid]
    # Fresh writes use the same monitor-only projection, while malformed JSON
    # remains unresolved and bounded page sizes are still required.
    conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('new', 'new', 'LONG', raw))
    assert conn.execute("SELECT state FROM protection_plan_activity WHERE config_id='new'").fetchone()[0] == 'DONE'
    with pytest.raises(ValueError, match='RUNTIME_SCAN_BATCH_SIZE'):
        protection_scan.active_plan_page(conn, limit=3)
    conn.close()


def test_monitor_plan_reconciliation_reads_large_owned_plan_without_bypassing_account_check(record_db, monkeypatch):
    _mode(monkeypatch, False)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    originals = []
    with database.get_db_conn() as conn:
        protection_scan.initialize_schema(conn.cursor())
        for symbol, scope in (('ETH/USDT:USDT', 'owned'), ('BTC/USDT:USDT', 'foreign')):
            raw = json.dumps({'symbol': symbol, 'side': 'LONG', 'state': 'WAITING',
                              'account_scope': scope, 'original_evidence': 'x' * 2000})
            originals.append(raw)
            conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('cfg', symbol, 'LONG', raw))
        conn.commit()
    service = object.__new__(PositionProtection)
    service.config_id, service.account_scope = 'cfg', 'owned'
    reconciled = []
    service._reconcile = lambda plan: reconciled.append(plan['symbol'])
    result = service.reconcile_plan_page(limit=2)
    assert reconciled == ['ETH/USDT:USDT']
    assert result['processed'] == 2 and result['next_cursor'] > 0
    assert result['results'][0]['error'] is None
    assert 'original account' in result['results'][1]['error']
    with database.get_db_conn() as conn:
        assert [row[0] for row in conn.execute('SELECT payload FROM real_protection_plans ORDER BY rowid')] == originals


def test_monitor_ownership_index_reads_large_originals_and_keeps_paging(record_db, monkeypatch):
    _mode(monkeypatch, False)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    exchange = Exchange([])
    baseline = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    raw = json.dumps({'state': 'DONE', 'symbol': baseline.symbol, 'side': 'LONG',
                      'account_scope': baseline.scope, 'entries': [{'id': 'original-entry'}],
                      'original_evidence': 'x' * 2000})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('2026-10-15 12:00:00', 'cfg', baseline.symbol, raw))
        conn.commit()
    for _ in range(32):
        resumed = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
        assert resumed.index_state['processed'] <= 2
        if resumed.index_state['complete']:
            break
    assert resumed.index_state['complete']
    with database.get_db_conn() as conn:
        link = conn.execute("SELECT config_id,role FROM execution_order_links WHERE order_id='original-entry'").fetchone()
        assert tuple(link) == ('cfg', 'entry')
        assert conn.execute('SELECT payload FROM real_protection_events').fetchone()[0] == raw


def test_monitor_cycle_record_work_history_and_output_gates_are_all_bypassed(record_db, monkeypatch):
    _mode(monkeypatch, False)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '6000')
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    ledger.ingest([fill(index) for index in range(1, 81)] +
                  [{**fill(81), 'order': 'close', 'side': 'sell', 'amount': 8, 'cost': 800}])
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT rowid,payload FROM execution_fills ORDER BY rowid LIMIT 1').fetchone()
        value = json.loads(row['payload'])
        value['original_evidence'] = 'x' * 20000
        raw = json.dumps(value)
        conn.execute('UPDATE execution_fills SET payload=? WHERE rowid=?', (raw, row['rowid']))
        for revision in range(60):
            stamp = datetime.fromtimestamp((100002 + revision) / 1000, tz=database.TZ_CN).replace(tzinfo=None).isoformat()
            plan = {'account_scope': ledger.scope, 'symbol': ledger.symbol, 'side': 'LONG',
                    'revision': revision, 'take_profit': 110 + revision, 'stop_loss': 90}
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                         (stamp, 'cfg', ledger.symbol, json.dumps(plan)))
        conn.commit()
    result = rebuild_execution_position_history('cfg', ledger.scope, ledger.symbol)
    assert result['scan_complete'] and result['published'] and result['projection_complete']
    assert result['completed_count'] == 1 and result['omitted_completed'] == 0
    cycle = result['completed'][0]
    assert len(cycle['events']) == 81 and len(cycle['protection_history']) == 60
    assert len(json.dumps(cycle).encode()) > 6000
    assert cycle['remaining_base'] == 0
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT payload FROM execution_fills ORDER BY rowid LIMIT 1').fetchone()[0] == raw
        assert conn.execute('SELECT count(*) FROM execution_position_history').fetchone()[0] == 1


def test_monitor_ownership_index_still_rejects_non_object_evidence(record_db, monkeypatch):
    _mode(monkeypatch, False)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '1')
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', '[1,2,3]'))
        conn.commit()
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    assert not ledger.index_state['complete'] and ledger.index_state['error']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_order_links').fetchone()[0] == 0
        assert conn.execute('SELECT payload FROM real_protection_events').fetchone()[0] == '[1,2,3]'


def test_monitor_cycle_publication_still_rejects_a_concurrently_changed_source(record_db, monkeypatch):
    from backend.utils import execution_ledger

    _mode(monkeypatch, False)
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '1')
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    ledger.ingest([fill(1), {**fill(2), 'order': 'close', 'side': 'sell'}])
    with database.get_db_conn() as conn:
        previous = [tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]
    reconstruct = execution_ledger._reconstruct_position_cycles

    def changed(*args, **kwargs):
        result = reconstruct(*args, **kwargs)
        assert result['scan_complete']
        with database.get_db_conn() as conn:
            conn.execute("UPDATE execution_fills SET payload=json_set(payload,'$.price',101)")
            conn.commit()
        return result

    monkeypatch.setattr(execution_ledger, '_reconstruct_position_cycles', changed)
    result = rebuild_execution_position_history('cfg', ledger.scope, ledger.symbol)
    assert not result['published'] and result['reason_code'] == 'cycle_source_changed'
    with database.get_db_conn() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')] == previous
