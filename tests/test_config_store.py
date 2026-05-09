import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.config_store as config_store


def create_runtime_tables(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE app_settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE agent_configs (
            config_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            mode TEXT NOT NULL,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE secret_store (
            scope TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            secret_key TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(scope, scope_id, secret_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE llm_providers (
            provider_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT NOT NULL,
            api_base TEXT,
            temperature REAL,
            role TEXT NOT NULL DEFAULT 'agent',
            extra_body TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE exchange_profiles (
            profile_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            exchange TEXT NOT NULL,
            market_type TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runtime.db"
        create_runtime_tables(self.db_path)
        self.env = patch.dict(
            os.environ,
            {
                "CONFIG_MASTER_KEY": "test-master-key",
                "BINANCE_API_KEY": "binance-key",
                "BINANCE_SECRET": "binance-secret",
                "LEVERAGE": "8",
                "ENABLE_SCHEDULER": "true",
                "TRADING_MODE": "STRATEGY",
                "LANGCHAIN_PROJECT": "test-project",
                "SYMBOL_CONFIGS": json.dumps(
                    [
                        {
                            "config_id": "btc-strategy",
                            "symbol": "BTC/USDT",
                            "mode": "STRATEGY",
                            "model": "gpt-4o-mini",
                            "api_key": "agent-api-key",
                            "okx_api_key": "okx-agent-key",
                            "okx_secret": "okx-agent-secret",
                            "prompt_file": "strategy.txt",
                            "run_interval": 60,
                            "leverage": 5,
                        }
                    ]
                ),
            },
            clear=False,
        )
        self.db_patch = patch.object(config_store, "DB_NAME", self.db_path)
        self.env.start()
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.env.stop()
        self.temp_dir.cleanup()

    def test_migrates_env_config_and_encrypts_secrets(self):
        config_store.ensure_runtime_config_initialized()

        snapshot = config_store.load_runtime_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source"], "db")
        self.assertEqual(snapshot["leverage"], 8)
        self.assertEqual(snapshot["agents"][0]["config_id"], "btc-strategy")
        self.assertEqual(snapshot["agents"][0]["api_key"], "agent-api-key")
        self.assertEqual(snapshot["agents"][0]["okx_api_key"], "okx-agent-key")
        self.assertEqual(snapshot["agents"][0]["okx_secret"], "okx-agent-secret")
        self.assertTrue(snapshot["llm_providers"])
        self.assertTrue(snapshot["exchange_profiles"])
        self.assertEqual(snapshot["agents"][0]["llm_provider_id"], snapshot["llm_providers"][0]["provider_id"])
        self.assertEqual(snapshot["agents"][0]["exchange_profile_id"], snapshot["exchange_profiles"][0]["profile_id"])
        self.assertIn("1M", snapshot["market_timeframes"])

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT encrypted_value FROM secret_store WHERE scope = 'global' AND secret_key = 'global_binance_api_key'"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], "binance-key")

        conn = sqlite3.connect(self.db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_configs)").fetchall()}
        conn.close()
        self.assertIn("sort_order", columns)

    def test_save_runtime_snapshot_updates_flags_and_clears_secret(self):
        config_store.ensure_runtime_config_initialized()
        management = config_store.load_management_snapshot()
        management["globals"]["enable_scheduler"] = False
        management["globals"]["secrets"]["global_binance_api_key"] = {"clear": True}
        management["agents"][0]["secrets"]["api_key"] = {"value": "replacement-agent-key"}
        management["agents"][0]["secrets"]["okx_api_key"] = {"value": "replacement-okx-key"}

        config_store.save_runtime_snapshot(management["globals"], management["agents"])
        snapshot = config_store.load_runtime_snapshot()

        self.assertFalse(snapshot["enable_scheduler"])
        self.assertIsNone(snapshot.get("global_binance_api_key"))
        self.assertEqual(snapshot["agents"][0]["api_key"], "replacement-agent-key")
        self.assertEqual(snapshot["agents"][0]["okx_api_key"], "replacement-okx-key")

    def test_provider_and_profile_payloads_resolve_runtime_fields(self):
        globals_payload = dict(config_store.DEFAULT_GLOBAL_SETTINGS)
        providers = [
            {
                "provider_id": "openai-main",
                "name": "OpenAI Main",
                "model": "gpt-4.1-mini",
                "api_base": "https://api.openai.com/v1",
                "temperature": 0.2,
                "role": "agent",
                "extra_body": {"reasoning_effort": "low"},
                "secrets": {"api_key": {"value": "provider-key"}},
            }
        ]
        profiles = [
            {
                "profile_id": "okx-main",
                "name": "OKX Main",
                "exchange": "okx",
                "market_type": "swap",
                "secrets": {
                    "api_key": {"value": "okx-key"},
                    "secret": {"value": "okx-secret"},
                    "passphrase": {"value": "okx-pass"},
                },
            }
        ]
        agents = [
            {
                "config_id": "btc-okx",
                "symbol": "BTC/USDT",
                "mode": "REAL",
                "llm_provider_id": "openai-main",
                "exchange_profile_id": "okx-main",
                "prompt_file": "strategy.txt",
            }
        ]

        config_store.save_runtime_snapshot(globals_payload, agents, providers, profiles)
        snapshot = config_store.load_runtime_snapshot()
        agent = snapshot["agents"][0]

        self.assertEqual(agent["model"], "gpt-4.1-mini")
        self.assertEqual(agent["api_base"], "https://api.openai.com/v1")
        self.assertEqual(agent["api_key"], "provider-key")
        self.assertEqual(agent["okx_api_key"], "okx-key")
        self.assertEqual(agent["okx_secret"], "okx-secret")
        self.assertEqual(agent["passphrase"], "okx-pass")
        self.assertEqual(agent["extra_body"], {"reasoning_effort": "low"})

    def test_provider_and_profile_secret_masks_and_clear(self):
        config_store.save_runtime_snapshot(
            dict(config_store.DEFAULT_GLOBAL_SETTINGS),
            [{"config_id": "btc", "symbol": "BTC/USDT", "mode": "STRATEGY", "llm_provider_id": "p1", "exchange_profile_id": "e1"}],
            [{"provider_id": "p1", "name": "P1", "model": "gpt", "secrets": {"api_key": {"value": "provider-secret"}}}],
            [{"profile_id": "e1", "name": "E1", "exchange": "binance", "market_type": "swap", "secrets": {"api_key": {"value": "key"}, "secret": {"value": "secret"}}}],
        )
        management = config_store.load_management_snapshot()
        self.assertTrue(management["llm_providers"][0]["secrets"]["api_key"]["configured"])
        self.assertTrue(management["exchange_profiles"][0]["secrets"]["secret"]["configured"])

        management["llm_providers"][0]["secrets"]["api_key"] = {"clear": True}
        config_store.save_runtime_snapshot(
            management["globals"],
            management["agents"],
            management["llm_providers"],
            management["exchange_profiles"],
        )
        snapshot = config_store.load_runtime_snapshot()
        self.assertIsNone(snapshot["llm_providers"][0].get("api_key"))

    def test_save_runtime_snapshot_preserves_agent_order(self):
        agents = [
            {"config_id": "eth-dca", "symbol": "ETH/USDT", "mode": "SPOT_DCA", "model": "gpt-4o-mini"},
            {"config_id": "btc-real", "symbol": "BTC/USDT", "mode": "REAL", "model": "gpt-4o-mini"},
            {"config_id": "btc-strategy", "symbol": "BTC/USDT", "mode": "STRATEGY", "model": "gpt-4o-mini"},
        ]

        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), agents)
        snapshot = config_store.load_runtime_snapshot()

        self.assertEqual(
            [agent["config_id"] for agent in snapshot["agents"]],
            ["eth-dca", "btc-real", "btc-strategy"],
        )

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT config_id, sort_order FROM agent_configs ORDER BY sort_order ASC").fetchall()
        conn.close()
        self.assertEqual(rows, [("eth-dca", 0), ("btc-real", 1), ("btc-strategy", 2)])

    def test_load_runtime_snapshot_defaults_to_symbol_and_mode_order_without_sort_order(self):
        timestamp = "2026-05-05 00:00:00"
        conn = sqlite3.connect(self.db_path)
        rows = [
            ("eth-dca", "ETH/USDT", 1, "SPOT_DCA", {"config_id": "eth-dca", "symbol": "ETH/USDT", "mode": "SPOT_DCA", "model": "gpt-4o-mini"}),
            ("btc-strategy", "BTC/USDT", 1, "STRATEGY", {"config_id": "btc-strategy", "symbol": "BTC/USDT", "mode": "STRATEGY", "model": "gpt-4o-mini"}),
            ("btc-real", "BTC/USDT", 1, "REAL", {"config_id": "btc-real", "symbol": "BTC/USDT", "mode": "REAL", "model": "gpt-4o-mini"}),
        ]
        for config_id, symbol, enabled, mode, payload in rows:
            conn.execute(
                """
                INSERT INTO agent_configs (config_id, symbol, enabled, mode, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (config_id, symbol, enabled, mode, json.dumps(payload), timestamp),
            )
        conn.commit()
        conn.close()

        snapshot = config_store.load_runtime_snapshot()

        self.assertEqual(
            [agent["config_id"] for agent in snapshot["agents"]],
            ["btc-real", "btc-strategy", "eth-dca"],
        )

    def test_load_runtime_snapshot_degrades_on_invalid_secret_key(self):
        timestamp = "2026-05-05 00:00:00"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO secret_store (scope, scope_id, secret_key, encrypted_value, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("global", "", "global_binance_api_key", "not-a-valid-fernet-token", timestamp),
        )
        conn.commit()
        conn.close()

        snapshot = config_store.load_runtime_snapshot()

        self.assertIsNone(snapshot)
        self.assertIn("CONFIG_MASTER_KEY", config_store.LAST_RUNTIME_CONFIG_ERROR or "")


if __name__ == "__main__":
    unittest.main()
