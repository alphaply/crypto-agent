"""Large history streams fully; oversized facts never replace a complete view."""
import json
from types import SimpleNamespace

from backend import database
from backend.utils import execution_cycles
from backend.utils.execution_ledger import ExecutionLedger, register_order, rebuild_execution_position_history, position_cycles
from test_execution_ledger import local_db, Exchange, fill


def test_many_cycles_publish_complete_projection(local_db,monkeypatch):
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
    assert result['omitted_completed']==0 and result['projection_complete']
    assert len(result['completed']) == 80
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0]==80
        last=json.loads(conn.execute('SELECT payload FROM execution_position_history ORDER BY closed_at_ms DESC LIMIT 1').fetchone()[0])
        assert last['remaining_base']==0 and len(last['events'])==2
    projected=position_cycles('cfg',0,100000000,ledger.scope,ledger.symbol)
    assert projected['completed_count']==80 and projected['omitted_completed']==0


def test_large_source_row_is_read_and_published(local_db,monkeypatch):
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    register_order(ledger.scope,ledger.symbol,'close','cfg','agent_exit',side='LONG')
    ledger.ingest([fill(1),{**fill(2),'order':'close','side':'sell'}])
    # Ingestion queues work; create the baseline via the background rebuild.
    assert rebuild_execution_position_history('cfg', ledger.scope, ledger.symbol)['published']
    with database.get_db_conn() as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]
        row=conn.execute('SELECT storage_id AS rowid,payload FROM execution_fills ORDER BY storage_id LIMIT 1').fetchone()
        value=json.loads(row['payload']);value['large_diagnostic']='x'*20000
        conn.execute('UPDATE execution_fills SET payload=%s WHERE storage_id=%s',(json.dumps(value),row['rowid']))
        conn.commit()
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES','6000')
    original_loads=json.loads
    def bounded_loads(text):
        return original_loads(text)
    monkeypatch.setattr(execution_cycles,'json',SimpleNamespace(loads=bounded_loads,dumps=json.dumps))
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert result['scan_complete'] and result['published']
    assert result['completed_count'] == 1 and result['reason_code'] is None
    with database.get_db_conn() as conn:
        published = json.loads(conn.execute('SELECT payload FROM execution_position_history').fetchone()[0])
        assert published['remaining_base'] == 0 and len(published['events']) == 2
        assert conn.execute('SELECT COUNT(*) FROM execution_position_history').fetchone()[0] == len(before)
        raw = json.loads(conn.execute('SELECT payload FROM execution_fills ORDER BY storage_id LIMIT 1').fetchone()[0])
        assert raw['large_diagnostic'] == 'x' * 20000


def test_large_single_cycle_retains_every_event(local_db,monkeypatch):
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES','6000')
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    ledger.ingest([fill(index) for index in range(1,80)])
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert result['scan_complete'] and result['reason_code'] is None
    assert result['published'] and result['completed_count'] == 0
    assert sum(len(cycle['events']) for cycle in result['open_cycles']) == 79


def test_concurrent_financial_source_change_can_write_and_prevents_stale_publication(local_db,monkeypatch):
    from backend.utils import execution_ledger
    ledger=ExecutionLedger(Exchange([]),'cfg','ETH/USDT')
    register_order(ledger.scope,ledger.symbol,'entry','cfg','entry',side='LONG')
    register_order(ledger.scope,ledger.symbol,'close','cfg','agent_exit',side='LONG')
    ledger.ingest([fill(1),{**fill(2),'order':'close','side':'sell'}])
    # Ingestion queues work; create the baseline via the background rebuild.
    assert rebuild_execution_position_history('cfg', ledger.scope, ledger.symbol)['published']
    with database.get_db_conn() as conn:
        before=[tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]
    original=execution_ledger._reconstruct_position_cycles
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        with database.get_db_conn() as conn:
            conn.execute("UPDATE execution_fills SET payload=jsonb_set(payload::jsonb,'{price}','101'::jsonb)::text")
            conn.commit()
        return result
    monkeypatch.setattr(execution_ledger,'_reconstruct_position_cycles',changed)
    result=rebuild_execution_position_history('cfg',ledger.scope,ledger.symbol)
    assert not result['published'] and result['reason_code']=='cycle_source_changed'
    with database.get_db_conn() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM execution_position_history')]==before


def test_protection_history_streams_snapshots_and_preserves_large_originals(local_db, monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    register_order(ledger.scope, ledger.symbol, 'entry', 'cfg', 'entry', side='LONG')
    register_order(ledger.scope, ledger.symbol, 'close', 'cfg', 'agent_exit', side='LONG')
    stamp = datetime.fromtimestamp(100, tz=database.TZ_CN).replace(tzinfo=None).isoformat()
    with database.get_db_conn() as conn:
        for revision in range(3):
            plan = dict(account_scope=ledger.scope, symbol=ledger.symbol, side='LONG',
                        revision=revision, stop_loss=90+revision, take_profit=110+revision)
            if revision == 2:
                plan['original_diagnostic'] = 'x' * 70000
            raw = json.dumps(plan)
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                         'SELECT %s,%s,%s,%s FROM generate_series(1,%s)',
                         (stamp, 'cfg', ledger.symbol, raw, 1 if revision == 2 else 500))
        conn.commit()
    original = execution_cycles.bounded_text
    audit_reads = []
    @contextmanager
    def tracked(conn, table, column, rowid, **kwargs):
        if table == 'real_protection_events':
            audit_reads.append(rowid)
        with original(conn, table, column, rowid, **kwargs) as value:
            yield value
    monkeypatch.setattr(execution_cycles, 'bounded_text', tracked)
    ledger.ingest([fill(1), {**fill(2), 'order':'close', 'side':'sell'}], refresh=False)
    result = rebuild_execution_position_history('cfg', ledger.scope, ledger.symbol)
    assert result['published'] and result['completed_count'] == 1
    cycle = result['completed'][0]
    assert cycle['stop_loss'] == 92 and cycle['take_profit'] == 112
    assert cycle['protection_history'][-1]['revision'] == 2
    assert len(audit_reads) == 1
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM real_protection_events').fetchone()[0] == 1001


def test_protection_episode_filter_preserves_unprojectable_raw_evidence(local_db):
    from datetime import datetime
    ledger = ExecutionLedger(Exchange([]), 'cfg', 'ETH/USDT')
    cycle = dict(account_scope=ledger.scope, symbol=ledger.symbol, side='LONG',
                 plan_episode_id='owned', opened_at_ms=100000, closed_at_ms=200000)
    with database.get_db_conn() as conn:
        for episode, scope, ts, revision, extra in [
            ('owned', ledger.scope, 90000, 1, ''),
            ('owned', ledger.scope, 150000, 2, '\x00'),
            ('owned', ledger.scope, 250000, 3, ''),
            ('other', ledger.scope, 170000, 4, ''),
            ('owned', 'foreign', 175000, 5, ''),
        ]:
            stamp = datetime.fromtimestamp(ts/1000, tz=database.TZ_CN).replace(tzinfo=None).isoformat()
            plan = dict(account_scope=scope, symbol=ledger.symbol, side='LONG',
                        episode_id=episode, revision=revision, stop_loss=90+revision,
                        take_profit=110+revision, diagnostic=extra)
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                         'VALUES(%s,%s,%s,%s)', (stamp, 'cfg', ledger.symbol, json.dumps(plan)))
        conn.commit()
    assert execution_cycles._protection(cycle, 'cfg', 4*1024*1024, None)
    assert [row['revision'] for row in cycle['protection_history']] == [1, 2]
    assert cycle['stop_loss'] == 92 and cycle['take_profit'] == 112
