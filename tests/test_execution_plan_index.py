import hashlib
import json

import pytest

from backend import database
from backend.database.schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger
from test_trade_execution_latency import drain


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
def index_db(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'index.db'))
    with database.get_db_conn() as conn:
        pass  # PostgreSQL migrations are installed by financial_pg.
        conn.commit()


def test_cross_page_regular_evidence_survives_slices_before_alias_retirement(index_db, monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    monkeypatch.setenv('RUNTIME_INDEX_SCAN_SECONDS', '.001')
    scope = hashlib.sha256(b'binance:index-fixture').hexdigest()
    plans = [dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE',
                  execution_mode='independent_exits', exits=[dict(id='7', order_type='stop_market')])]
    plans.extend(dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE') for _ in range(4))
    plans.append(dict(account_scope=scope, symbol='ETH/USDT:USDT', side='LONG', state='DONE', entries=[dict(id='7')]))
    original = [json.dumps(plan) for plan in plans]
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO execution_order_links (account_scope,symbol,order_id,config_id,role,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                     (scope, 'ETH/USDT:USDT', '7', 'cfg', 'stop_loss', '{}'))
        for payload in original:
            conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                         ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', payload))
        conn.commit()
    exchange = Exchange()
    first = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    assert not first.index_state['complete']
    assert first.index_saved_plans()['processed'] <= 2
    drain(first)
    resumed = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    assert resumed.index_state['complete'] and resumed.index_state['processed'] == 0
    with database.get_db_conn() as conn:
        assert conn.execute("SELECT role FROM execution_order_links WHERE order_id='7'").fetchone()[0] == 'entry'
        assert [row[0] for row in conn.execute('SELECT payload FROM real_protection_events ORDER BY id')] == original
        assert conn.execute('SELECT count(*) FROM execution_plan_index_records').fetchone()[0] == len(plans)
    assert not exchange.calls


def test_large_source_is_loaded_and_sync_completes_despite_legacy_byte_setting(index_db, monkeypatch):
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    raw = json.dumps({'state': 'DONE', 'large': 'x' * 2000})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    from backend.utils import execution_plan_index
    exchange = Exchange()
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    drain(ledger)
    assert ledger.index_state['complete'] and not ledger.index_state['error']
    result = ledger.sync(start_ms=0, now_ms=100000)
    assert result['complete'] and exchange.calls
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT payload FROM real_protection_events').fetchone()[0] == raw


def test_empty_baseline_plan_does_not_rebuild_unaffected_owned_history(index_db, monkeypatch):
    raw = json.dumps({'state': 'DONE', 'entries': [], 'exits': []})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    import backend.utils.execution_ledger as ledger_module
    monkeypatch.setattr(ledger_module, 'rebuild_execution_position_history',
                        lambda *_: pytest.fail('No ownership links changed in an empty baseline'))
    drain(ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT'))


def test_history_publication_queues_rebuild_instead_of_running_it_inline(index_db, monkeypatch):
    raw = json.dumps({'state': 'DONE', 'entries': [{'id': 'entry'}], 'exits': []})
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) VALUES(%s,%s,%s,%s)',
                     ('2026-10-15 12:00:00', 'cfg', 'ETH/USDT:USDT', raw))
        conn.commit()
    import backend.utils.execution_ledger as ledger_module
    from backend.utils import execution_plan_index
    clock, rebuilt = [0], []
    monkeypatch.setattr(execution_plan_index, 'monotonic', lambda: clock[0])
    def rebuild(*args):
        rebuilt.append(args)
        clock[0] += 1  # The single bounded record finished after its soft budget.
        return {'complete': True}
    monkeypatch.setattr(ledger_module, 'rebuild_execution_position_history', rebuild)
    drain(ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT'))
    assert not rebuilt
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM execution_history_work').fetchone()[0] == 1


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield


def test_slow_pending_scan_still_publishes_and_unblocks_memory_statistics(index_db, monkeypatch):
    """A scan slower than the soft slice must not starve publication forever."""
    import time
    from backend.utils import execution_plan_index as index
    from backend.utils.execution_history_work import drain_scope_history
    from backend.agent.memory_service import format_recent_position_history_for_memory

    ledger = ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT')
    plan = dict(account_scope=ledger.scope, symbol=ledger.symbol, side='LONG',
                entries=[dict(id='entry', price=100)], exit_order=dict(id='close'))
    with database.get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_events(timestamp,config_id,symbol,payload) '
                     'VALUES(%s,%s,%s,%s)',
                     ('2026-10-15 12:00:00', 'cfg', ledger.symbol, json.dumps(plan)))
        conn.commit()
    clock = [0.0]
    pending = index._pending
    def slow_scan(*args, **kwargs):
        result = pending(*args, **kwargs)
        clock[0] += 1.0  # Exceeds the 0.25 second slice on every scan.
        return result
    monkeypatch.setattr(index, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(index, '_pending', slow_scan)
    for _ in range(6):
        if ledger.index_saved_plans()['complete']:
            break
    assert ledger.index_state['complete']

    now = int(time.time() * 1000)
    ledger.ingest([
        dict(id='1', order='entry', timestamp=now-2000, price=100, amount=1,
             side='buy', cost=100, fee=dict(currency='USDT', cost=.1)),
        dict(id='2', order='close', timestamp=now-1000, price=110, amount=1,
             side='sell', cost=110, fee=dict(currency='USDT', cost=.1),
             info=dict(realizedPnl='10')),
    ], refresh=False)
    drain_scope_history(ledger.scope, ledger.symbol)
    rows = database.get_closed_positions_7d('cfg', ledger.symbol, mode='REAL')
    assert len(rows) == 1 and rows[0]['realized_pnl'] == 10
    text = format_recent_position_history_for_memory('cfg', dict(symbol=ledger.symbol, mode='REAL'))
    assert '100.0%' in text and '暂无已重建' not in text


def test_slow_source_query_uses_database_budget_not_expired_slice(index_db, monkeypatch):
    from contextlib import contextmanager
    from backend.utils import execution_plan_index as index
    ledger = ExecutionLedger(Exchange(), 'cfg', 'ETH/USDT')
    clock = [0.0]
    original = index.get_db_conn
    budgets = []
    @contextmanager
    def connection(timeout=30):
        budgets.append(timeout)
        clock[0] += 1.0
        with original(timeout=timeout) as conn:
            yield conn
    monkeypatch.setattr(index, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(index, 'get_db_conn', connection)
    assert ledger.index_saved_plans()['complete']
    assert all(budget == 30 for budget in budgets)
