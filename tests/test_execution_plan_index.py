import hashlib
import json

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger


class Exchange:
    id, apiKey = 'binance', 'index-fixture'
    markets = {'ETH/USDT:USDT': {'symbol': 'ETH/USDT:USDT', 'contract': True, 'swap': True, 'linear': True}}
    calls = 0
    def market(self, _):
        return self.markets['ETH/USDT:USDT']
    def fetch_my_trades(self, *args, **kwargs):
        self.calls += 1
        return []


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'index.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        conn.commit()


def test_cross_page_regular_evidence_precedes_alias_retirement_and_restart_resumes(index_db, monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    scope = hashlib.sha256(b'binance:index-fixture').hexdigest()
    plans = [dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE',
                  execution_mode='independent_exits', exits=[dict(id='7', order_type='stop_market')])]
    plans.extend(dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE') for _ in range(4))
    plans.append(dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE', entries=[dict(id='7')]))
    original = [json.dumps(plan) for plan in plans]
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?)',
                     (scope, 'ETH/USDT:USDT', '7', 'cfg', 'stop_loss', '{}'))
        for payload in original:
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                         ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', payload))
        conn.commit()
    exchange = Exchange()
    first = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    assert not first.index_state['complete']
    assert first.index_state['processed'] <= 2
    pending = first.sync(now_ms=100000, start_ms=0)
    assert not pending['complete'] and not exchange.calls
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT role FROM execution_order_links WHERE order_id='7'").fetchone()[0] == 'stop_loss'
    for _ in range(30):
        resumed = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
        assert resumed.index_state['processed'] <= 2
        if resumed.index_state['complete']:
            break
    assert resumed.index_state['complete']
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT role FROM execution_order_links WHERE order_id='7'").fetchone()[0] == 'entry'
        assert [row[0] for row in conn.execute('SELECT payload FROM real_protection_events ORDER BY id')] == original
        assert conn.execute('SELECT count(*) FROM execution_plan_index_records').fetchone()[0] == 0
    assert not exchange.calls


def test_oversized_source_is_not_loaded_and_sync_remains_explicitly_incomplete(index_db, monkeypatch):
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    raw = json.dumps({'state': 'DONE', 'large': 'x' * 2000})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    from backend.utils import execution_plan_index
    monkeypatch.setattr(execution_plan_index, '_load', lambda *_: pytest.fail('oversized payload fetched'))
    exchange = Exchange()
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    assert not ledger.index_state['complete'] and ledger.index_state['error']
    result = ledger.sync(start_ms=0, now_ms=100000)
    assert not result['complete'] and not exchange.calls
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT payload FROM real_protection_events').fetchone()[0] == raw


def test_empty_baseline_plan_does_not_rebuild_unaffected_owned_history(index_db, monkeypatch):
    raw = json.dumps({'state': 'DONE', 'entries': [], 'exits': []})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    import backend.utils.execution_ledger as ledger_module
    monkeypatch.setattr(ledger_module, 'rebuild_execution_position_history',
                        lambda *_: pytest.fail('No ownership links changed in an empty baseline'))
    assert ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT').index_state['complete']


def test_final_history_publication_marks_complete_even_if_its_record_used_time_budget(index_db, monkeypatch):
    raw = json.dumps({'state': 'DONE', 'entries': [{'id': 'entry'}], 'exits': []})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(?,?,?,?)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    import backend.utils.execution_ledger as ledger_module
    from backend.utils import execution_plan_index
    clock, rebuilt = [0], []
    monkeypatch.setattr(execution_plan_index.time, 'monotonic', lambda: clock[0])
    def rebuild(*args):
        rebuilt.append(args)
        clock[0] += 1  # The single bounded record finished after its soft budget.
        return {'complete': True}
    monkeypatch.setattr(ledger_module, 'rebuild_execution_position_history', rebuild)
    assert ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT').index_state['complete']
    assert len(rebuilt) == 1
