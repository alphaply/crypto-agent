from unittest.mock import patch

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.utils.execution_ledger import ExecutionLedger


class Exchange:
    id = 'okx'
    markets = {'ETH/USDT:USDT': {'symbol': 'ETH/USDT:USDT', 'contract': True, 'swap': True, 'linear': True}}
    apiKey = 'bounded-progress-test'

    def __init__(self, count=1001):
        self.trades = [dict(id=str(index), order='external', timestamp=100000 + index,
                            amount=.001, price=100, cost=.1, side='buy')
                       for index in range(count)]
        self.calls = []
        self.fail = False

    def load_markets(self):
        pass

    def market(self, symbol):
        return dict(symbol='ETH/USDT:USDT', contract=True, swap=True, linear=True)

    def fetch_my_trades(self, symbol, since, limit, params):
        self.calls.append((since, params['until']))
        if self.fail:
            raise RuntimeError('offline')
        return [trade for trade in self.trades if since <= trade['timestamp'] <= params['until']][:limit]


@pytest.fixture
def local_db(tmp_path):
    with patch.object(database, 'DB_NAME', str(tmp_path / 'progress.db')):
        with database.get_db_conn() as conn:
            initialize_schema(conn)
        yield


def test_small_budgets_resume_completed_leaves_across_restart(local_db):
    exchange = Exchange()
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    first = ledger.sync(now_ms=200000, start_ms=40000, max_calls=4)
    assert not first['complete'] and first['calls'] == 4
    assert first['covered_intervals'] and first['pending_windows']
    completed = tuple(first['covered_intervals'][0])
    already_fetched = exchange.calls.count(completed)
    for _ in range(30):
        restarted = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
        state = restarted.sync(now_ms=200000, start_ms=40000, max_calls=4)
        assert state['calls'] <= 4
        if state['complete']:
            break
    assert state['complete'] and state['pending_windows'] == []
    assert exchange.calls.count(completed) == already_fetched
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 1001


def test_completed_partitions_make_dense_overlap_refresh_bounded_and_find_late_fills(local_db):
    exchange = Exchange()
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    assert ledger.sync(now_ms=120000, start_ms=40000)['complete']
    initial_calls = len(exchange.calls)
    exchange.trades.append({**exchange.trades[5], 'id': 'delayed-new-id', 'timestamp': 100500})
    state = ExecutionLedger(exchange, 'cfg', 'ETH/USDT').sync(now_ms=120001, start_ms=40000, max_calls=20)
    assert state['complete']
    assert len(exchange.calls) - initial_calls <= 20
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 1002


def test_failed_page_retains_frontier_and_never_advances_coverage(local_db):
    exchange = Exchange()
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    first = ledger.sync(now_ms=200000, start_ms=40000, max_calls=4)
    assert first['pending_windows']
    exchange.fail = True
    failed = ExecutionLedger(exchange, 'cfg', 'ETH/USDT').sync(now_ms=200000, start_ms=40000, max_calls=4)
    assert failed['calls'] == 1 and failed['error'] == 'offline'
    assert failed['covered_intervals'] == first['covered_intervals']
    assert failed['through_ms'] == first['through_ms']
    assert failed['pending_windows'] == first['pending_windows']


def test_same_millisecond_overflow_remains_incomplete_and_recoverable(local_db):
    exchange = Exchange(101)
    for trade in exchange.trades:
        trade['timestamp'] = 100000
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    result = ledger.sync(now_ms=100000, start_ms=100000, max_calls=2)
    assert not result['complete']
    assert 'one millisecond' in result['error']
    assert result['pending_windows'] == [[100000, 100000]]
    assert not result.get('covered_intervals')


def test_full_page_outside_time_bounds_is_not_accepted_as_coverage(local_db):
    exchange = Exchange(100)
    exchange.fetch_my_trades = lambda *args, **kwargs: exchange.trades
    ledger = ExecutionLedger(exchange, 'cfg', 'ETH/USDT')
    result = ledger.sync(now_ms=200000, start_ms=150000, max_calls=4)
    assert not result['complete'] and 'time window' in result['error']
    assert result['pending_windows'] == [[150000, 200000]]
    assert not result.get('covered_intervals')
