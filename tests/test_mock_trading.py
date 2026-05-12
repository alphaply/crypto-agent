import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database


def create_mock_trading_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE mock_accounts (
            config_id TEXT PRIMARY KEY,
            symbol TEXT,
            balance REAL DEFAULT 10000.0,
            failures INTEGER DEFAULT 0
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE mock_balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id TEXT,
            symbol TEXT,
            timestamp TEXT,
            balance REAL,
            unrealized_pnl REAL DEFAULT 0,
            total_equity REAL
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE mock_orders (
            order_id TEXT PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            agent_name TEXT,
            config_id TEXT,
            side TEXT,
            type TEXT,
            price REAL,
            amount REAL,
            stop_loss REAL,
            take_profit REAL,
            expire_at REAL,
            status TEXT DEFAULT 'OPEN',
            close_price REAL,
            realized_pnl REAL,
            close_time TEXT,
            is_filled INTEGER DEFAULT 0
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            timestamp TEXT,
            symbol TEXT,
            agent_name TEXT,
            config_id TEXT,
            trade_mode TEXT,
            side TEXT,
            entry_price REAL,
            amount REAL,
            take_profit REAL,
            stop_loss REAL,
            reason TEXT,
            status TEXT DEFAULT 'OPEN'
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


class MockTradingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mock_trading.db"
        create_mock_trading_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_get_mock_account_initializes_default_balance(self):
        account = database.get_mock_account("cfg-a", "BTC/USDT")

        self.assertEqual(account["config_id"], "cfg-a")
        self.assertEqual(account["symbol"], "BTC/USDT")
        self.assertEqual(account["balance"], 10000.0)
        self.assertEqual(account["failures"], 0)

    def test_update_mock_account_balance_resets_after_bankruptcy(self):
        database.get_mock_account("cfg-a", "BTC/USDT")

        balance, failures = database.update_mock_account_balance("cfg-a", "BTC/USDT", -9500)

        with database.get_db_conn() as conn:
            history_row = conn.execute(
                "SELECT balance, unrealized_pnl, total_equity FROM mock_balance_history WHERE config_id = ?",
                ("cfg-a",),
            ).fetchone()

        self.assertEqual(balance, 10000.0)
        self.assertEqual(failures, 1)
        self.assertEqual(history_row["balance"], 10000.0)
        self.assertEqual(history_row["unrealized_pnl"], 0.0)
        self.assertEqual(history_row["total_equity"], 10000.0)

    def test_create_fill_and_close_mock_order_updates_related_state(self):
        database.get_mock_account("cfg-a", "BTC/USDT")
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)",
                ("MO-1", "BTC/USDT", "cfg-a", "OPEN"),
            )
            conn.commit()

        database.create_mock_order(
            symbol="BTC/USDT",
            side="BUY",
            price=100,
            amount=2,
            stop_loss=90,
            take_profit=120,
            agent_name="agent-a",
            config_id="cfg-a",
            order_id="MO-1",
        )
        database.update_mock_order_filled("MO-1")

        filled = database.get_filled_mock_positions("cfg-a", symbol="BTC/USDT")
        database.close_mock_order("MO-1", close_price=110, realized_pnl=20)

        with database.get_db_conn() as conn:
            mock_order = conn.execute(
                "SELECT status, close_price, realized_pnl, close_time, is_filled FROM mock_orders WHERE order_id = ?",
                ("MO-1",),
            ).fetchone()
            order_log = conn.execute(
                "SELECT status FROM orders WHERE order_id = ?",
                ("MO-1",),
            ).fetchone()
            account = conn.execute(
                "SELECT balance, failures FROM mock_accounts WHERE config_id = ?",
                ("cfg-a",),
            ).fetchone()
            position = conn.execute(
                "SELECT status, close_price, realized_pnl FROM position_history WHERE config_id = ? AND position_key = ?",
                ("cfg-a", "MO-1"),
            ).fetchone()

        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["order_id"], "MO-1")
        self.assertEqual(mock_order["status"], "CLOSED")
        self.assertEqual(mock_order["close_price"], 110)
        self.assertEqual(mock_order["realized_pnl"], 20)
        self.assertEqual(mock_order["is_filled"], 1)
        self.assertTrue(mock_order["close_time"])
        self.assertEqual(order_log["status"], "CLOSED")
        self.assertEqual(account["balance"], 10020.0)
        self.assertEqual(account["failures"], 0)
        self.assertEqual(position["status"], "CLOSED")
        self.assertEqual(position["close_price"], 110)
        self.assertEqual(position["realized_pnl"], 20)

    def test_create_mock_order_falls_back_to_agent_name_for_config_id(self):
        database.create_mock_order(
            symbol="ETH/USDT",
            side="SELL",
            price=200,
            amount=1,
            stop_loss=210,
            take_profit=180,
            agent_name="legacy-agent",
            config_id=None,
            order_id="MO-LEGACY",
        )

        rows_by_config = database.get_mock_orders(symbol="ETH/USDT", config_id="legacy-agent")
        rows_by_agent = database.get_mock_orders(symbol="ETH/USDT", agent_name="legacy-agent")

        self.assertEqual(len(rows_by_config), 1)
        self.assertEqual(len(rows_by_agent), 1)
        self.assertEqual(rows_by_config[0]["config_id"], "legacy-agent")

    def test_auto_close_expired_mock_orders_only_cancels_unfilled_rows(self):
        past = time.time() - 60
        future = time.time() + 3600
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO mock_orders (order_id, timestamp, symbol, agent_name, config_id, side, price, amount, expire_at, status, is_filled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("MO-EXP", "2026-05-12 09:00:00", "BTC/USDT", "agent-a", "cfg-a", "BUY", 100, 1, past, "OPEN", 0),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, timestamp, symbol, agent_name, config_id, side, price, amount, expire_at, status, is_filled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("MO-FILLED", "2026-05-12 09:00:00", "BTC/USDT", "agent-a", "cfg-a", "BUY", 100, 1, past, "OPEN", 1),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, timestamp, symbol, agent_name, config_id, side, price, amount, expire_at, status, is_filled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("MO-FUTURE", "2026-05-12 09:00:00", "BTC/USDT", "agent-a", "cfg-a", "BUY", 100, 1, future, "OPEN", 0),
            )
            conn.execute("INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)", ("MO-EXP", "BTC/USDT", "cfg-a", "OPEN"))
            conn.execute("INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)", ("MO-FILLED", "BTC/USDT", "cfg-a", "OPEN"))
            conn.execute("INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)", ("MO-FUTURE", "BTC/USDT", "cfg-a", "OPEN"))
            conn.commit()

        closed = database.auto_close_expired_mock_orders(config_id="cfg-a", symbol="BTC/USDT")

        with database.get_db_conn() as conn:
            statuses = {
                row["order_id"]: (row["status"], row["close_time"])
                for row in conn.execute(
                    "SELECT order_id, status, close_time FROM mock_orders ORDER BY order_id"
                ).fetchall()
            }
            order_statuses = {
                row["order_id"]: row["status"]
                for row in conn.execute(
                    "SELECT order_id, status FROM orders ORDER BY order_id"
                ).fetchall()
            }

        self.assertEqual(closed, 1)
        self.assertEqual(statuses["MO-EXP"][0], "CANCELLED")
        self.assertTrue(statuses["MO-EXP"][1])
        self.assertEqual(statuses["MO-FILLED"][0], "OPEN")
        self.assertEqual(statuses["MO-FUTURE"][0], "OPEN")
        self.assertEqual(order_statuses["MO-EXP"], "CANCELLED")
        self.assertEqual(order_statuses["MO-FILLED"], "OPEN")
        self.assertEqual(order_statuses["MO-FUTURE"], "OPEN")


if __name__ == "__main__":
    unittest.main()