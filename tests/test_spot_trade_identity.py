import pytest
from backend import storage
from unittest.mock import Mock

from backend.database.trade import TradeHistoryStore


def test_native_trade_identity_keeps_two_assets_and_task_ownership(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    with storage.connect() as conn:
        conn.execute("INSERT INTO trade_history(trade_id, order_id, symbol, amount) VALUES('7','order7','BTC/USDT',1)")
    store = TradeHistoryStore(lambda: storage.connect(), Mock())
    trade = {'id': '7', 'order': 'order7', 'symbol': 'BTC/USDT', 'timestamp': 1790899200000,
             'side': 'buy', 'price': 100, 'amount': 1, 'cost': 100}
    store.save_trades([trade, {**trade, 'symbol': 'ETH/USDT'}], config_id='task')
    store.save_trades([trade, {**trade, 'symbol': 'ETH/USDT'}], config_id='task')
    # An unscoped poll must not duplicate an already owned fill.
    store.save_trades([trade])
    with storage.connect() as conn:
        rows = conn.execute('SELECT symbol,config_id,amount FROM trade_history ORDER BY symbol').fetchall()
    assert [tuple(row) for row in rows] == [('BTC/USDT', 'task', 1), ('ETH/USDT', 'task', 1)]


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
