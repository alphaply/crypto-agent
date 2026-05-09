import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.utils.prompt_utils import resolve_prompt_file_content
from backend.app.services import dashboard_service


def create_daily_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symbol TEXT,
            config_id TEXT,
            summary TEXT,
            source_count INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(date, config_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE short_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_start TEXT NOT NULL,
            bucket_end TEXT NOT NULL,
            symbol TEXT,
            config_id TEXT NOT NULL,
            market_summary TEXT,
            position_summary TEXT,
            source_count INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(config_id, bucket_start)
        )
        """
    )
    conn.execute(
        """
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
        """
    )
    conn.execute(
        """
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            agent_name TEXT,
            config_id TEXT,
            agent_type TEXT,
            timeframe TEXT,
            content TEXT,
            strategy_logic TEXT
        )
        """
    )
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
    conn.commit()
    conn.close()


class DailySummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "daily.db"
        create_daily_table(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_delete_daily_summary_targets_date_and_config(self):
        database.save_daily_summary("2026-05-05", "BTC/USDT", "btc-a", "A", 1)
        database.save_daily_summary("2026-05-05", "BTC/USDT", "btc-b", "B", 1)
        database.save_daily_summary("2026-05-06", "BTC/USDT", "btc-a", "C", 1)

        deleted = database.delete_daily_summary("2026-05-05", "btc-a")
        rows = database.list_daily_summaries(symbol="BTC/USDT", limit=10)

        self.assertEqual(deleted, 1)
        self.assertEqual({(row["date"], row["config_id"]) for row in rows}, {("2026-05-05", "btc-b"), ("2026-05-06", "btc-a")})

    def test_short_memory_upserts_by_config_and_bucket(self):
        database.save_short_memory("2026-05-06 00:00:00", "2026-05-06 04:00:00", "BTC/USDT", "btc-a", "A", "P", 2)
        database.save_short_memory("2026-05-06 00:00:00", "2026-05-06 04:00:00", "BTC/USDT", "btc-a", "B", "Q", 3)

        row = database.get_short_memory("btc-a", "2026-05-06 00:00:00")
        rows = database.get_short_memories("btc-a", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(row["market_summary"], "B")
        self.assertEqual(row["position_summary"], "Q")
        self.assertEqual(row["source_count"], 3)

    def test_position_history_keeps_closed_position(self):
        database.upsert_position_history(
            config_id="btc-a",
            symbol="BTC/USDT",
            position_key="mock-1",
            side="LONG",
            status="OPEN",
            source="mock_order",
            opened_at="2026-05-06 00:00:00",
            entry_price=100,
            amount=1,
        )
        database.upsert_position_history(
            config_id="btc-a",
            symbol="BTC/USDT",
            position_key="mock-1",
            side="LONG",
            status="CLOSED",
            source="mock_order",
            closed_at="2026-05-06 01:00:00",
            close_price=110,
            amount=1,
            realized_pnl=10,
        )

        rows = database.get_position_history("btc-a", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[0]["realized_pnl"], 10)

    def test_dashboard_data_hides_disabled_configs(self):
        class RuntimeConfig:
            def get_all_symbol_configs(self):
                return [
                    {"config_id": "enabled", "symbol": "BTC/USDT", "enabled": True, "mode": "STRATEGY", "model": "m1"},
                    {"config_id": "disabled", "symbol": "BTC/USDT", "enabled": False, "mode": "STRATEGY", "model": "m2"},
                ]

            def get_leverage(self, config_id=None):
                return 10

        with patch.object(dashboard_service, "global_config", RuntimeConfig()):
            rows = dashboard_service.get_dashboard_data("BTC/USDT")

        self.assertEqual([row["config_id"] for row in rows], ["enabled"])

    def test_prompt_file_fallback_when_missing(self):
        class Logger:
            def warning(self, *_args, **_kwargs):
                pass

        content = resolve_prompt_file_content("missing.txt", self.db_path.parent, Logger(), fallback="fallback {content}")

        self.assertEqual(content, "fallback {content}")


if __name__ == "__main__":
    unittest.main()
