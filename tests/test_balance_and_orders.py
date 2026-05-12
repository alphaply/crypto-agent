import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database


def create_balance_and_order_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
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
    conn.commit()
    conn.close()


class BalanceAndOrdersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "balance_orders.db"
        create_balance_and_order_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_save_balance_snapshot_persists_total_equity_and_config(self):
        database.save_balance_snapshot("BTC/USDT", 1000, 25, config_id="cfg-a")

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT symbol, config_id, total_balance, unrealized_pnl, total_equity FROM balance_history"
            ).fetchone()

        self.assertEqual(row["symbol"], "BTC/USDT")
        self.assertEqual(row["config_id"], "cfg-a")
        self.assertEqual(row["total_balance"], 1000)
        self.assertEqual(row["unrealized_pnl"], 25)
        self.assertEqual(row["total_equity"], 1025)

    def test_get_balance_history_prefers_config_rows_then_falls_back_to_symbol(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO balance_history (timestamp, symbol, config_id, total_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-05-10 10:00:00", "BTC/USDT", None, 900, 0, 900),
            )
            conn.execute(
                "INSERT INTO balance_history (timestamp, symbol, config_id, total_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-05-11 10:00:00", "BTC/USDT", None, 950, 0, 950),
            )
            conn.execute(
                "INSERT INTO balance_history (timestamp, symbol, config_id, total_balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-05-12 10:00:00", "BTC/USDT", "cfg-a", 1000, 50, 1050),
            )
            conn.commit()

        preferred = database.get_balance_history("BTC/USDT", limit=10, config_id="cfg-a")
        fallback = database.get_balance_history("BTC/USDT", limit=10, config_id="cfg-missing")

        self.assertEqual([row["config_id"] for row in preferred], ["cfg-a"])
        self.assertEqual([row["total_equity"] for row in preferred], [1050])
        self.assertEqual([row["config_id"] for row in fallback], [None, None, "cfg-a"])
        self.assertEqual([row["total_equity"] for row in fallback], [900, 950, 1050])

    def test_get_paginated_orders_returns_desc_rows_and_total_count(self):
        with database.get_db_conn() as conn:
            conn.execute("INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)", ("o-1", "cfg-a", "BTC/USDT", "OPEN"))
            conn.execute("INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)", ("o-2", "cfg-a", "BTC/USDT", "CLOSED"))
            conn.execute("INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)", ("o-3", "cfg-a", "BTC/USDT", "OPEN"))
            conn.execute("INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)", ("o-x", "cfg-b", "ETH/USDT", "OPEN"))
            conn.commit()

        rows, total = database.get_paginated_orders("cfg-a", page=1, per_page=2)
        rows_page_2, total_page_2 = database.get_paginated_orders("cfg-a", page=2, per_page=2)

        self.assertEqual(total, 3)
        self.assertEqual(total_page_2, 3)
        self.assertEqual([row["order_id"] for row in rows], ["o-3", "o-2"])
        self.assertEqual([row["order_id"] for row in rows_page_2], ["o-1"])


if __name__ == "__main__":
    unittest.main()