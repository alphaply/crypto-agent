import sqlite3
from unittest.mock import Mock

from backend.database_trade import TradeHistoryStore


def test_legacy_trade_id_upgrade_keeps_two_assets_and_task_ownership(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE trade_history (
            trade_id TEXT PRIMARY KEY, order_id TEXT, config_id TEXT, timestamp TEXT,
            symbol TEXT, side TEXT, price REAL, amount REAL, cost REAL, fee REAL,
            fee_currency TEXT, realized_pnl REAL)''')
        conn.execute("INSERT INTO trade_history(trade_id, order_id, symbol, amount) VALUES('7','order7','BTC/USDT',1)")
    store = TradeHistoryStore(lambda: sqlite3.connect(path), Mock())
    trade = {'id': '7', 'order': 'order7', 'symbol': 'BTC/USDT', 'timestamp': 1790899200000,
             'side': 'buy', 'price': 100, 'amount': 1, 'cost': 100}
    store.save_trades([trade, {**trade, 'symbol': 'ETH/USDT'}], config_id='task')
    store.save_trades([trade, {**trade, 'symbol': 'ETH/USDT'}], config_id='task')
    # An unscoped poll must not duplicate an already owned fill.
    store.save_trades([trade])
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT symbol,config_id,amount FROM trade_history ORDER BY symbol').fetchall()
    assert rows == [('BTC/USDT', 'task', 1), ('ETH/USDT', 'task', 1)]
