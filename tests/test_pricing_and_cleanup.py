import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.database_cleanup import ConfigCleanupStore


def create_pricing_and_cleanup_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE model_pricing (
            model TEXT PRIMARY KEY,
            input_price_per_m REAL DEFAULT 0,
            output_price_per_m REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD'
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE chat_sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            config_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        '''
    )
    conn.execute("CREATE TABLE mock_accounts (config_id TEXT PRIMARY KEY, symbol TEXT, balance REAL, failures INTEGER)")
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
            close_time TEXT
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
        '''
    )
    conn.execute(
        '''
        CREATE TABLE token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            config_id TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER
        )
        '''
    )
    conn.execute(
        '''
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
        '''
    )
    conn.execute(
        '''
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


class PricingAndCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pricing_cleanup.db"
        create_pricing_and_cleanup_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_model_pricing_upsert_and_delete_round_trip(self):
        database.update_model_pricing("gpt-4.1", 1.2, 2.4, "USD")
        database.update_model_pricing("gpt-4.1", 1.5, 2.7, "CNY")

        pricing = database.get_all_pricing()
        deleted = database.delete_model_pricing("gpt-4.1")

        self.assertEqual(pricing["gpt-4.1"]["input_price_per_m"], 1.5)
        self.assertEqual(pricing["gpt-4.1"]["output_price_per_m"], 2.7)
        self.assertEqual(pricing["gpt-4.1"]["currency"], "CNY")
        self.assertEqual(deleted, 1)
        self.assertEqual(database.get_all_pricing(), {})

    def test_dependency_counts_include_open_order_totals(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?, ?)",
                ("s1", "Title", "cfg-a", "BTC/USDT", "2026-05-12 09:00:00", "2026-05-12 09:00:00"),
            )
            conn.execute("INSERT INTO mock_accounts VALUES (?, ?, ?, ?)", ("cfg-a", "BTC/USDT", 10000, 0))
            conn.execute(
                "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                ("cfg-a", "BTC/USDT", "2026-05-12 09:00:00", 10000, 0, 10000),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                ("mo1", "cfg-a", "BTC/USDT", "OPEN"),
            )
            conn.execute(
                "INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                ("o1", "cfg-a", "BTC/USDT", "OPEN"),
            )
            conn.execute("INSERT INTO summaries (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
            conn.execute("INSERT INTO token_usage (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
            conn.execute(
                "INSERT INTO daily_summaries (date, symbol, config_id, summary, created_at) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-12", "BTC/USDT", "cfg-a", "summary", "2026-05-12 09:00:00"),
            )
            conn.execute(
                "INSERT INTO short_memories (bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12 08:00:00", "2026-05-12 12:00:00", "BTC/USDT", "cfg-a", "m", "p", "2026-05-12 09:00:00"),
            )
            conn.execute(
                "INSERT INTO position_history (config_id, symbol, position_key, status, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("cfg-a", "BTC/USDT", "pk1", "OPEN", "2026-05-12 09:00:00"),
            )
            conn.commit()

        counts = database.get_config_dependency_counts("cfg-a")

        self.assertEqual(counts["chat_sessions"], 1)
        self.assertEqual(counts["mock_orders"], 1)
        self.assertEqual(counts["orders"], 1)
        self.assertEqual(counts["open_mock_orders"], 1)
        self.assertEqual(counts["open_orders"], 1)
        self.assertEqual(counts["position_history"], 1)

    def test_soft_delete_runtime_data_keeps_history_but_closes_open_rows(self):
        cleanup_store = ConfigCleanupStore(database.get_db_conn, lambda: "2026-05-12 13:00:00")
        with patch.object(database, "_config_cleanup_store", cleanup_store):
            with database.get_db_conn() as conn:
                conn.execute(
                    "INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?, ?)",
                    ("s1", "Title", "cfg-a", "BTC/USDT", "2026-05-12 09:00:00", "2026-05-12 09:00:00"),
                )
                conn.execute("INSERT INTO mock_accounts VALUES (?, ?, ?, ?)", ("cfg-a", "BTC/USDT", 10000, 0))
                conn.execute(
                    "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                    ("cfg-a", "BTC/USDT", "2026-05-12 09:00:00", 10000, 0, 10000),
                )
                conn.execute(
                    "INSERT INTO mock_orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                    ("mo-open", "cfg-a", "BTC/USDT", "OPEN"),
                )
                conn.execute(
                    "INSERT INTO mock_orders (order_id, config_id, symbol, status, close_time) VALUES (?, ?, ?, ?, ?)",
                    ("mo-closed", "cfg-a", "BTC/USDT", "CLOSED", "2026-05-11 09:00:00"),
                )
                conn.execute(
                    "INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                    ("o-open", "cfg-a", "BTC/USDT", "OPEN"),
                )
                conn.execute(
                    "INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                    ("o-closed", "cfg-a", "BTC/USDT", "CLOSED"),
                )
                conn.execute("INSERT INTO summaries (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
                conn.execute("INSERT INTO token_usage (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
                conn.execute(
                    "INSERT INTO daily_summaries (date, symbol, config_id, summary, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("2026-05-12", "BTC/USDT", "cfg-a", "summary", "2026-05-12 09:00:00"),
                )
                conn.commit()

            result = database.soft_delete_config_runtime_data("cfg-a")

            with database.get_db_conn() as conn:
                remaining_summary = conn.execute("SELECT COUNT(*) FROM summaries WHERE config_id = ?", ("cfg-a",)).fetchone()[0]
                remaining_token_usage = conn.execute("SELECT COUNT(*) FROM token_usage WHERE config_id = ?", ("cfg-a",)).fetchone()[0]
                remaining_daily_summaries = conn.execute("SELECT COUNT(*) FROM daily_summaries WHERE config_id = ?", ("cfg-a",)).fetchone()[0]
                open_mock = conn.execute("SELECT status, close_time FROM mock_orders WHERE order_id = ?", ("mo-open",)).fetchone()
                closed_order = conn.execute("SELECT status FROM orders WHERE order_id = ?", ("o-open",)).fetchone()
                removed_sessions = conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE config_id = ?", ("cfg-a",)).fetchone()[0]

        self.assertEqual(result["chat_sessions_deleted"], 1)
        self.assertEqual(result["mock_accounts_deleted"], 1)
        self.assertEqual(result["mock_balance_history_deleted"], 1)
        self.assertEqual(result["open_mock_orders_closed"], 1)
        self.assertEqual(result["open_orders_cancelled"], 1)
        self.assertEqual(remaining_summary, 1)
        self.assertEqual(remaining_token_usage, 1)
        self.assertEqual(remaining_daily_summaries, 1)
        self.assertEqual(open_mock[0], "CLOSED")
        self.assertEqual(open_mock[1], "2026-05-12 13:00:00")
        self.assertEqual(closed_order[0], "CANCELLED")
        self.assertEqual(removed_sessions, 0)

    def test_purge_config_all_data_deletes_all_supported_tables(self):
        cleanup_store = ConfigCleanupStore(database.get_db_conn, lambda: "2026-05-12 14:00:00")
        with patch.object(database, "_config_cleanup_store", cleanup_store):
            with database.get_db_conn() as conn:
                conn.execute(
                    "INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?, ?)",
                    ("s1", "Title", "cfg-a", "BTC/USDT", "2026-05-12 09:00:00", "2026-05-12 09:00:00"),
                )
                conn.execute("INSERT INTO mock_accounts VALUES (?, ?, ?, ?)", ("cfg-a", "BTC/USDT", 10000, 0))
                conn.execute(
                    "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                    ("cfg-a", "BTC/USDT", "2026-05-12 09:00:00", 10000, 0, 10000),
                )
                conn.execute(
                    "INSERT INTO mock_orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                    ("mo1", "cfg-a", "BTC/USDT", "OPEN"),
                )
                conn.execute(
                    "INSERT INTO orders (order_id, config_id, symbol, status) VALUES (?, ?, ?, ?)",
                    ("o1", "cfg-a", "BTC/USDT", "OPEN"),
                )
                conn.execute("INSERT INTO summaries (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
                conn.execute("INSERT INTO token_usage (config_id, symbol) VALUES (?, ?)", ("cfg-a", "BTC/USDT"))
                conn.execute(
                    "INSERT INTO daily_summaries (date, symbol, config_id, summary, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("2026-05-12", "BTC/USDT", "cfg-a", "summary", "2026-05-12 09:00:00"),
                )
                conn.execute(
                    "INSERT INTO short_memories (bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("2026-05-12 08:00:00", "2026-05-12 12:00:00", "BTC/USDT", "cfg-a", "m", "p", "2026-05-12 09:00:00"),
                )
                conn.execute(
                    "INSERT INTO position_history (config_id, symbol, position_key, status, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("cfg-a", "BTC/USDT", "pk1", "OPEN", "2026-05-12 09:00:00"),
                )
                conn.commit()

            result = database.purge_config_all_data("cfg-a")
            counts = database.get_config_dependency_counts("cfg-a")

        self.assertEqual(result["chat_sessions_deleted"], 1)
        self.assertEqual(result["mock_accounts_deleted"], 1)
        self.assertEqual(result["mock_balance_history_deleted"], 1)
        self.assertEqual(result["mock_orders_deleted"], 1)
        self.assertEqual(result["orders_deleted"], 1)
        self.assertEqual(result["summaries_deleted"], 1)
        self.assertEqual(result["token_usage_deleted"], 1)
        self.assertEqual(result["daily_summaries_deleted"], 1)
        self.assertEqual(result["short_memories_deleted"], 1)
        self.assertEqual(result["position_history_deleted"], 1)
        self.assertTrue(all(value == 0 for value in counts.values()))


if __name__ == "__main__":
    unittest.main()