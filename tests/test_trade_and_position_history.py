import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database


def create_trade_and_position_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE trade_history (
            trade_id TEXT PRIMARY KEY,
            order_id TEXT,
            config_id TEXT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            price REAL,
            amount REAL,
            cost REAL,
            fee REAL,
            fee_currency TEXT,
            realized_pnl REAL
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            config_id TEXT,
            total_balance REAL,
            unrealized_pnl REAL,
            total_equity REAL
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE position_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id TEXT NOT NULL,
            symbol TEXT,
            position_key TEXT NOT NULL,
            side TEXT,
            status TEXT,
            source TEXT,
            opened_at TEXT,
            closed_at TEXT,
            entry_price REAL,
            close_price REAL,
            amount REAL,
            realized_pnl REAL,
            raw_json TEXT,
            updated_at TEXT,
            UNIQUE(config_id, position_key)
        )
        '''
    )
    conn.commit()
    conn.close()


class TradeAndPositionHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "trade_position.db"
        create_trade_and_position_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_save_trade_history_ignores_duplicates_and_uses_info_fallbacks(self):
        trades = [
            {
                "id": "t-1",
                "order": "o-1",
                "timestamp": 1747044000000,
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 100,
                "amount": 2,
                "cost": 200,
                "fee": {"cost": 0.5, "currency": "USDT"},
                "info": {"realizedPnl": "12.34"},
            },
            {
                "id": "t-1",
                "order": "o-1-dup",
                "timestamp": 1747045000000,
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": 101,
                "amount": 1,
                "cost": 101,
            },
            {
                "id": "t-2",
                "order_id": "o-2",
                "config_id": "cfg-from-trade",
                "timestamp": 1747047600000,
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": 110,
                "amount": 1,
                "cost": 110,
                "realizedPnl": 5,
            },
        ]

        database.save_trade_history(trades, config_id=None)

        rows = database.get_trade_history("BTC/USDT", limit=10)

        self.assertEqual([row["trade_id"] for row in rows], ["t-2", "t-1"])
        self.assertEqual(rows[1]["order_id"], "o-1")
        self.assertEqual(rows[1]["fee"], 0.5)
        self.assertEqual(rows[1]["fee_currency"], "USDT")
        self.assertEqual(rows[1]["realized_pnl"], 12.34)
        self.assertEqual(rows[0]["config_id"], "cfg-from-trade")

    def test_clean_financial_data_removes_balance_and_trade_rows_for_symbol(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO balance_history (timestamp, symbol, total_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-12 10:00:00", "BTC/USDT", 1000, 0, 1000),
            )
            conn.execute(
                "INSERT INTO balance_history (timestamp, symbol, total_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-12 10:00:00", "ETH/USDT", 2000, 0, 2000),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, symbol, timestamp, side, price, amount, cost) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t-btc", "BTC/USDT", "2026-05-12 10:00:00", "buy", 100, 1, 100),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, symbol, timestamp, side, price, amount, cost) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("t-eth", "ETH/USDT", "2026-05-12 10:00:00", "buy", 200, 1, 200),
            )
            conn.commit()

        deleted = database.clean_financial_data("BTC/USDT")

        with database.get_db_conn() as conn:
            btc_balance = conn.execute("SELECT COUNT(*) FROM balance_history WHERE symbol = ?", ("BTC/USDT",)).fetchone()[0]
            eth_balance = conn.execute("SELECT COUNT(*) FROM balance_history WHERE symbol = ?", ("ETH/USDT",)).fetchone()[0]
            btc_trade = conn.execute("SELECT COUNT(*) FROM trade_history WHERE symbol = ?", ("BTC/USDT",)).fetchone()[0]
            eth_trade = conn.execute("SELECT COUNT(*) FROM trade_history WHERE symbol = ?", ("ETH/USDT",)).fetchone()[0]

        self.assertEqual(deleted, 2)
        self.assertEqual(btc_balance, 0)
        self.assertEqual(eth_balance, 1)
        self.assertEqual(btc_trade, 0)
        self.assertEqual(eth_trade, 1)

    def test_get_position_history_filters_since_time(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO position_history (config_id, symbol, position_key, side, status, source, opened_at, entry_price, amount, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("cfg-a", "BTC/USDT", "old", "LONG", "OPEN", "exchange_position", "2026-05-10 00:00:00", 100, 1, "2026-05-10 00:00:00"),
            )
            conn.execute(
                "INSERT INTO position_history (config_id, symbol, position_key, side, status, source, opened_at, closed_at, entry_price, close_price, amount, realized_pnl, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("cfg-a", "BTC/USDT", "new", "SHORT", "CLOSED", "exchange_trade", "2026-05-12 00:00:00", "2026-05-12 12:00:00", 120, 110, 2, 20, "2026-05-12 12:00:00"),
            )
            conn.commit()

        rows = database.get_position_history("cfg-a", since_time="2026-05-11 00:00:00", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_key"], "new")
        self.assertEqual(rows[0]["status"], "CLOSED")

    def test_sync_open_position_history_skips_zero_amount_and_upserts_open_position(self):
        positions = [
            {"side": "long", "amount": 0, "entry_price": 100},
            {"side": "long", "contracts": 2, "entryPrice": 105, "foo": "bar"},
        ]

        database.sync_open_position_history("cfg-a", "BTC/USDT", positions)

        rows = database.get_position_history("cfg-a", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_key"], "exchange_position:BTC/USDT:LONG:105.0")
        self.assertEqual(rows[0]["status"], "OPEN")
        self.assertEqual(rows[0]["entry_price"], 105.0)
        self.assertEqual(rows[0]["amount"], 2.0)

    def test_sync_trade_position_history_ignores_zero_pnl_and_uses_position_side(self):
        trades = [
            {
                "id": "skip-me",
                "timestamp": 1747044000000,
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 100,
                "amount": 1,
                "realizedPnl": 0,
            },
            {
                "id": "keep-me",
                "timestamp": 1747047600000,
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": 110,
                "amount": 1.5,
                "info": {"realizedPnl": "8.5", "positionSide": "SHORT"},
            },
        ]

        database.sync_trade_position_history("cfg-a", "BTC/USDT", trades)

        rows = database.get_position_history("cfg-a", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_key"], "exchange_trade:keep-me")
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[0]["side"], "SHORT")
        self.assertEqual(rows[0]["close_price"], 110)
        self.assertEqual(rows[0]["realized_pnl"], 8.5)


if __name__ == "__main__":
    unittest.main()