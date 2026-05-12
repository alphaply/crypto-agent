import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.app.services import dashboard_service


def create_activity_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE trade_history (
            trade_id TEXT,
            order_id TEXT,
            timestamp TEXT,
            symbol TEXT,
            config_id TEXT,
            side TEXT,
            price REAL,
            amount REAL,
            cost REAL,
            fee REAL,
            fee_currency TEXT,
            realized_pnl REAL
        )
        """
    )
    conn.commit()
    conn.close()


class RecentOrderActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "activity.db"
        create_activity_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_recent_activity_collapses_duplicate_cancel_logs(self):
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "BUY",
            100,
            110,
            90,
            "open reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            amount=0.2,
            status="OPEN",
        )
        with database.get_db_conn() as conn:
            conn.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", ("ST-1",))
            conn.commit()
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "CANCEL_BUY",
            0,
            0,
            0,
            "cancel reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            status="CANCELLED",
        )
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "CANCEL",
            0,
            0,
            0,
            "duplicate cancel reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            status="CANCELLED",
        )

        payload = dashboard_service.get_recent_order_activity_payload("cfg-a", limit=10)

        self.assertEqual(payload["total"], 1)
        row = payload["orders"][0]
        self.assertEqual(row["order_id"], "ST-1")
        self.assertEqual(row["action_label"], "撤单")
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(row["entry_price"], 100)
        self.assertEqual(row["amount"], 0.2)
        self.assertEqual(row["take_profit"], 110)
        self.assertEqual(row["stop_loss"], 90)


if __name__ == "__main__":
    unittest.main()