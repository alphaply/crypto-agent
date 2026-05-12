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
    conn.execute(
        """
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

    def test_save_daily_summary_upserts_and_get_daily_summaries_orders_desc(self):
        database.save_daily_summary("2026-05-05", "BTC/USDT", "btc-a", "first", 1)
        database.save_daily_summary("2026-05-06", "BTC/USDT", "btc-a", "second", 2)
        database.save_daily_summary("2026-05-05", "BTC/USDT", "btc-a", "updated", 3)

        rows = database.get_daily_summaries("btc-a", days=10)

        self.assertEqual([(row["date"], row["summary"], row["source_count"]) for row in rows], [
            ("2026-05-06", "second", 2),
            ("2026-05-05", "updated", 3),
        ])

    def test_summary_logic_queries_filter_by_config_and_time_window(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO summaries (timestamp, symbol, agent_name, config_id, strategy_logic) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-05 01:00:00", "BTC/USDT", "a", "btc-a", "logic-a1"),
            )
            conn.execute(
                "INSERT INTO summaries (timestamp, symbol, agent_name, config_id, strategy_logic) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-05 05:00:00", "BTC/USDT", "a", "btc-a", "logic-a2"),
            )
            conn.execute(
                "INSERT INTO summaries (timestamp, symbol, agent_name, config_id, strategy_logic) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-05 08:00:00", "BTC/USDT", "b", "btc-b", "logic-b1"),
            )
            conn.execute(
                "INSERT INTO summaries (timestamp, symbol, agent_name, config_id, strategy_logic) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-05 09:00:00", "BTC/USDT", "a", "btc-a", ""),
            )
            conn.commit()

        pending = database.get_pending_daily_summary_data("btc-a", "2026-05-05")
        window_rows = database.get_summary_logic_between("btc-a", "2026-05-05 02:00:00", "2026-05-05 10:00:00")

        self.assertEqual([row["strategy_logic"] for row in pending], ["logic-a1", "logic-a2", ""])
        self.assertEqual([row["strategy_logic"] for row in window_rows], ["logic-a2"])

    def test_short_memory_upserts_by_config_and_bucket(self):
        database.save_short_memory("2026-05-06 00:00:00", "2026-05-06 04:00:00", "BTC/USDT", "btc-a", "A", "P", 2)
        database.save_short_memory("2026-05-06 00:00:00", "2026-05-06 04:00:00", "BTC/USDT", "btc-a", "B", "Q", 3)

        row = database.get_short_memory("btc-a", "2026-05-06 00:00:00")
        rows = database.get_short_memories("btc-a", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(row["market_summary"], "B")
        self.assertEqual(row["position_summary"], "Q")
        self.assertEqual(row["source_count"], 3)

    def test_short_memory_update_and_listing_keep_desc_order(self):
        database.save_short_memory("2026-05-06 00:00:00", "2026-05-06 04:00:00", "BTC/USDT", "btc-a", "A", "P", 2)
        database.save_short_memory("2026-05-06 04:00:00", "2026-05-06 08:00:00", "BTC/USDT", "btc-a", "B", "Q", 3)

        updated = database.update_short_memory("btc-a", "2026-05-06 00:00:00", "AA", "PP")
        rows = database.get_short_memories("btc-a", limit=10)
        listed = database.list_short_memories(symbol="BTC/USDT", config_id="btc-a", limit=10)

        self.assertEqual(updated, 1)
        self.assertEqual([row["bucket_start"] for row in rows], ["2026-05-06 04:00:00", "2026-05-06 00:00:00"])
        self.assertEqual(rows[1]["market_summary"], "AA")
        self.assertEqual(rows[1]["position_summary"], "PP")
        self.assertEqual([row["bucket_start"] for row in listed], ["2026-05-06 04:00:00", "2026-05-06 00:00:00"])

    def test_summary_queries_respect_config_agent_type_and_pagination(self):
        database.save_summary("BTC/USDT", "agent-a", "c1", "logic-1", config_id="cfg-a", agent_type="trend")
        database.save_summary("BTC/USDT", "agent-a", "c2", "logic-2", config_id="cfg-a", agent_type="trend")
        database.save_summary("BTC/USDT", "agent-b", "c3", "logic-3", config_id="cfg-b", agent_type="mean")

        recent_cfg = database.get_recent_summaries("BTC/USDT", config_id="cfg-a", limit=10)
        recent_type = database.get_recent_summaries("BTC/USDT", agent_type="mean", limit=10)
        total_cfg = database.get_summary_count("BTC/USDT", config_id="cfg-a")
        paged_cfg = database.get_paginated_summaries("BTC/USDT", page=1, per_page=1, config_id="cfg-a")
        active_agents = sorted(database.get_active_agents("BTC/USDT"))

        self.assertEqual([row["strategy_logic"] for row in recent_cfg], ["logic-2", "logic-1"])
        self.assertEqual([row["strategy_logic"] for row in recent_type], ["logic-3"])
        self.assertEqual(total_cfg, 2)
        self.assertEqual(len(paged_cfg), 1)
        self.assertEqual(paged_cfg[0]["strategy_logic"], "logic-2")
        self.assertEqual(active_agents, ["cfg-a", "cfg-b"])

    def test_delete_summaries_by_symbol_removes_summary_order_and_mock_order_rows(self):
        database.save_summary("BTC/USDT", "agent-a", "content", "logic", config_id="cfg-a")
        database.save_summary("ETH/USDT", "agent-b", "other", "logic-eth", config_id="cfg-b")
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)",
                ("o-btc", "BTC/USDT", "cfg-a", "OPEN"),
            )
            conn.execute(
                "INSERT INTO orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)",
                ("o-eth", "ETH/USDT", "cfg-b", "OPEN"),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)",
                ("m-btc", "BTC/USDT", "cfg-a", "OPEN"),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, symbol, config_id, status) VALUES (?, ?, ?, ?)",
                ("m-eth", "ETH/USDT", "cfg-b", "OPEN"),
            )
            conn.commit()

        deleted = database.delete_summaries_by_symbol("BTC/USDT")

        with database.get_db_conn() as conn:
            btc_summary = conn.execute("SELECT COUNT(*) FROM summaries WHERE symbol = ?", ("BTC/USDT",)).fetchone()[0]
            eth_summary = conn.execute("SELECT COUNT(*) FROM summaries WHERE symbol = ?", ("ETH/USDT",)).fetchone()[0]
            btc_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE symbol = ?", ("BTC/USDT",)).fetchone()[0]
            eth_orders = conn.execute("SELECT COUNT(*) FROM orders WHERE symbol = ?", ("ETH/USDT",)).fetchone()[0]
            btc_mock_orders = conn.execute("SELECT COUNT(*) FROM mock_orders WHERE symbol = ?", ("BTC/USDT",)).fetchone()[0]
            eth_mock_orders = conn.execute("SELECT COUNT(*) FROM mock_orders WHERE symbol = ?", ("ETH/USDT",)).fetchone()[0]

        self.assertEqual(deleted, 1)
        self.assertEqual(btc_summary, 0)
        self.assertEqual(eth_summary, 1)
        self.assertEqual(btc_orders, 0)
        self.assertEqual(eth_orders, 1)
        self.assertEqual(btc_mock_orders, 0)
        self.assertEqual(eth_mock_orders, 1)

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
