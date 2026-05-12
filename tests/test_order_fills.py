import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database


def create_order_fill_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
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
            status TEXT DEFAULT 'OPEN',
            filled_amount REAL DEFAULT 0,
            filled_cost REAL DEFAULT 0,
            avg_fill_price REAL DEFAULT 0,
            filled_at TEXT
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE spot_order_fills (
            order_id TEXT PRIMARY KEY,
            config_id TEXT,
            symbol TEXT,
            status TEXT,
            filled_qty REAL DEFAULT 0,
            filled_cost REAL DEFAULT 0,
            avg_fill_price REAL DEFAULT 0,
            filled_at TEXT,
            last_sync_at TEXT
        )
        '''
    )
    conn.commit()
    conn.close()


class OrderFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "order_fill.db"
        create_order_fill_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_save_order_log_normalizes_trade_mode_and_config_fallback(self):
        database.save_order_log(
            order_id="ORD-1",
            symbol="BTC/USDT",
            agent_name="agent-a",
            side="buy",
            entry=100,
            tp=110,
            sl=90,
            reason="test",
            trade_mode="INVALID",
            config_id=None,
            amount=1.5,
            status="OPEN",
        )

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT config_id, trade_mode, amount, status FROM orders WHERE order_id = ?",
                ("ORD-1",),
            ).fetchone()

        self.assertEqual(row["config_id"], "agent-a")
        self.assertEqual(row["trade_mode"], "STRATEGY")
        self.assertEqual(row["amount"], 1.5)
        self.assertEqual(row["status"], "OPEN")

    def test_update_order_fill_status_updates_fill_fields(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO orders (order_id, status) VALUES (?, ?)",
                ("ORD-2", "OPEN"),
            )
            conn.commit()

        database.update_order_fill_status("ORD-2", "PARTIAL", 0.3, 30, 100, "2026-05-12 10:00:00")

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT status, filled_amount, filled_cost, avg_fill_price, filled_at FROM orders WHERE order_id = ?",
                ("ORD-2",),
            ).fetchone()

        self.assertEqual(row["status"], "PARTIAL")
        self.assertEqual(row["filled_amount"], 0.3)
        self.assertEqual(row["filled_cost"], 30)
        self.assertEqual(row["avg_fill_price"], 100)
        self.assertEqual(row["filled_at"], "2026-05-12 10:00:00")

    def test_upsert_spot_order_fill_overwrites_existing_row(self):
        database.upsert_spot_order_fill("ORD-3", "cfg-a", "BTC/USDT", "OPEN", 0, 0, 0, None)
        database.upsert_spot_order_fill("ORD-3", "cfg-a", "BTC/USDT", "FILLED", 2, 210, 105, "2026-05-12 11:00:00")

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT status, filled_qty, filled_cost, avg_fill_price, filled_at, last_sync_at FROM spot_order_fills WHERE order_id = ?",
                ("ORD-3",),
            ).fetchone()

        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["filled_qty"], 2)
        self.assertEqual(row["filled_cost"], 210)
        self.assertEqual(row["avg_fill_price"], 105)
        self.assertEqual(row["filled_at"], "2026-05-12 11:00:00")
        self.assertTrue(row["last_sync_at"])


if __name__ == "__main__":
    unittest.main()