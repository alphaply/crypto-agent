"""Large history streams fully; oversized facts never replace a complete view."""
import json
from types import SimpleNamespace

from backend import database
from backend.utils import execution_cycles
from backend.utils.execution_ledger import ExecutionLedger, register_order, rebuild_execution_position_history, position_cycles
from test_execution_ledger import local_db, Exchange, fill


def test_many_complete_cycles_publish_all_while_response_projection_stays_bounded(local_db,monkeypatch):
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES','6000')
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    register_order(ledger.scope,ledger.symbol,'close','cfg','agent_exit',side='LONG')
    trades=[]
    for index in range(80):
        trades.extend([fill(index*2+1),{**fill(index*2+2),'order':'close','side':'sell'}])
    ledger.ingest(trades)
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert result['scan_complete'] and result['published']
    assert result['completed_count']==80
    assert result['omitted_completed']>0 and not result['projection_complete']
    assert len(json.dumps(result['completed']).encode('utf-8'))<=6000
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0]==80
        last=json.loads(conn.execute('SELECT payload FROM execution_position_history ORDER BY closed_at_ms DESC LIMIT 1').fetchone()[0])
        assert last['remaining_base']==0 and len(last['events'])==2
    projected=position_cycles('cfg',0,100000000,ledger.scope,ledger.symbol)
    assert projected['completed_count']==80 and projected['omitted_completed']>0


def test_single_oversized_source_row_is_not_loaded_or_published(local_db,monkeypatch):
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    register_order(ledger.scope,ledger.symbol,'close','cfg','agent_exit',side='LONG')
    ledger.ingest([fill(1),{**fill(2),'order':'close','side':'sell'}])
    with database.get_db_conn() as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]
        row=conn.execute('SELECT rowid,payload FROM execution_fills ORDER BY rowid LIMIT 1').fetchone()
        value=json.loads(row['payload']);value['large_diagnostic']='x'*20000
        conn.execute('UPDATE execution_fills SET payload=? WHERE rowid=?',(json.dumps(value),row['rowid']))
        conn.commit()
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES','6000')
    original_loads=json.loads
    def bounded_loads(text):
        assert len(text.encode('utf-8'))<=6000
        return original_loads(text)
    monkeypatch.setattr(execution_cycles,'json',SimpleNamespace(loads=bounded_loads,dumps=json.dumps))
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert not result['scan_complete'] and not result['published'] and result['retained_previous']
    assert result['completed_count'] is None and result['reason_code']=='cycle_record_budget'
    with database.get_db_conn() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]==before


def test_unbounded_single_cycle_stops_before_its_event_array_grows(local_db,monkeypatch):
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES','6000')
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    ledger.ingest([fill(index) for index in range(1,80)])
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert not result['scan_complete'] and result['reason_code']=='cycle_working_set_budget'
    assert not result['published'] and result['completed_count'] is None
    assert sum(len(cycle['events']) for cycle in result['open_cycles'])<80


def test_concurrent_financial_source_change_can_write_and_prevents_stale_publication(local_db,monkeypatch):
    from backend.utils import execution_ledger
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    register_order(ledger.scope,ledger.symbol,'close','cfg','agent_exit',side='LONG')
    ledger.ingest([fill(1),{**fill(2),'order':'close','side':'sell'}])
    with database.get_db_conn() as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]
    original=execution_ledger._reconstruct_position_cycles
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        with database.get_db_conn() as conn:
            conn.execute("UPDATE execution_fills SET payload=json_set(payload,'$.price',101)")
            conn.commit()
        return result
    monkeypatch.setattr(execution_ledger,'_reconstruct_position_cycles',changed)
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert not result['published'] and result['reason_code']=='cycle_source_changed'
    with database.get_db_conn() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]==before
