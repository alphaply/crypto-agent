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

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT encrypted_value FROM secret_store WHERE scope = 'global' AND secret_key = 'global_binance_api_key'"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], "binance-key")

    def test_save_runtime_snapshot_updates_flags_and_clears_secret(self):
        config_store.ensure_runtime_config_initialized()
        management = config_store.load_management_snapshot()
        management["globals"]["enable_scheduler"] = False
        management["globals"]["secrets"]["global_binance_api_key"] = {"clear": True}
        management["agents"][0]["secrets"]["api_key"] = {"value": "replacement-agent-key"}

        config_store.save_runtime_snapshot(management["globals"], management["agents"])
        snapshot = config_store.load_runtime_snapshot()

        self.assertFalse(snapshot["enable_scheduler"])
        self.assertIsNone(snapshot.get("global_binance_api_key"))
        self.assertEqual(snapshot["agents"][0]["api_key"], "replacement-agent-key")


if __name__ == "__main__":
    unittest.main()
