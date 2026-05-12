import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.database_dca import DcaSnapshotStore


def create_dca_snapshot_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE dca_daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT,
            config_id TEXT,
            symbol TEXT,
            total_invested REAL,
            total_qty REAL,
            avg_cost REAL,
            buy_count INTEGER,
            first_buy TEXT,
            last_buy TEXT,
            actual_balance REAL,
            updated_at TEXT,
            UNIQUE(snapshot_date, config_id)
        )
        '''
    )
    conn.commit()
    conn.close()


class DcaSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "dca.db"
        create_dca_snapshot_table(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_dca_snapshot_upserts_same_day(self):
        store = DcaSnapshotStore(database.get_db_conn, lambda: "2026-05-12", lambda: "2026-05-12 10:00:00")
        with patch.object(database, "_dca_snapshot_store", store):
            database.save_dca_daily_snapshot(
                "cfg-a",
                "BTC/USDT",
                {"total_invested": 100, "total_qty": 1, "avg_cost": 100, "buy_count": 1, "actual_balance": 1},
            )
            database.save_dca_daily_snapshot(
                "cfg-a",
                "BTC/USDT",
                {"total_invested": 250, "total_qty": 2, "avg_cost": 125, "buy_count": 2, "actual_balance": 2},
            )

            rows = database.get_dca_daily_snapshot_history("cfg-a", days=30)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshot_date"], "2026-05-12")
        self.assertEqual(rows[0]["total_invested"], 250)
        self.assertEqual(rows[0]["total_qty"], 2)
        self.assertEqual(rows[0]["avg_cost"], 125)
        self.assertEqual(rows[0]["buy_count"], 2)
        self.assertEqual(rows[0]["actual_balance"], 2)

    def test_dca_snapshot_history_orders_ascending_and_respects_limit(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO dca_daily_snapshots (snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost, buy_count, actual_balance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-10", "cfg-a", "BTC/USDT", 100, 1, 100, 1, 1, "2026-05-10 10:00:00"),
            )
            conn.execute(
                "INSERT INTO dca_daily_snapshots (snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost, buy_count, actual_balance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-11", "cfg-a", "BTC/USDT", 200, 2, 100, 2, 2, "2026-05-11 10:00:00"),
            )
            conn.execute(
                "INSERT INTO dca_daily_snapshots (snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost, buy_count, actual_balance, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12", "cfg-a", "BTC/USDT", 300, 3, 100, 3, 3, "2026-05-12 10:00:00"),
            )
            conn.commit()

        rows = database.get_dca_daily_snapshot_history("cfg-a", days=2)

        self.assertEqual([row["snapshot_date"] for row in rows], ["2026-05-10", "2026-05-11"])


if __name__ == "__main__":
    unittest.main()