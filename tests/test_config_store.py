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
        self.assertEqual(snapshot["market_timeframes"], ["15m", "1h", "4h", "1d", "1w"])
        self.assertEqual(snapshot["langchain_project"], "test-project")

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

    def test_redacted_full_export_excludes_bootstrap_and_runtime_secrets(self):
        config_store.ensure_runtime_config_initialized()
        with patch.dict(os.environ, {'ADMIN_PASSWORD': 'admin-export-secret', 'JWT_SECRET': 'jwt-export-secret', 'PORT': '7860'}):
            exported = config_store.export_full_snapshot(include_secrets=False)
            encoded = json.dumps(exported)
            for secret in ('test-master-key', 'admin-export-secret', 'jwt-export-secret', 'binance-secret', 'agent-api-key', 'okx-agent-secret'):
                self.assertNotIn(secret, encoded)
            self.assertEqual(exported['env']['PORT'], '7860')
            self.assertEqual(exported['global_secrets'], {})
            self.assertEqual(config_store.export_full_snapshot(include_secrets=True)['env']['ADMIN_PASSWORD'], 'admin-export-secret')

    def test_full_import_rolls_back_runtime_when_mcp_validation_fails(self):
        from backend.mcp.transfer import import_settings
        config_store.ensure_runtime_config_initialized()
        before = config_store.load_runtime_snapshot()
        exported = config_store.export_full_snapshot(include_secrets=False)
        exported['app_settings']['leverage'] = 3
        invalid_mcp = {'profiles': [{'profile_id': 'mcp-test', 'name': 'Test', 'exchange_profile_id': 'missing-account', 'symbols': ['BTC/USDT']}]}
        with self.assertRaisesRegex(ValueError, 'missing or incompatible'):
            config_store.import_full_snapshot(exported, transaction_hook=lambda conn: import_settings(invalid_mcp, connection=conn))
        self.assertEqual(config_store.load_runtime_snapshot(), before)

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
                "system_prompt_role": "user",
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
        self.assertEqual(agent["system_prompt_role"], "user")

    def test_summarizer_provider_resolves_role_and_request_options(self):
        providers = [{
            "provider_id": "deepseek-summary", "name": "Summary provider", "role": "summarizer",
            "model": "deepseek-flash", "api_base": "https://summary.example.test/v1",
            "temperature": 0.2, "compatibility_mode": "deepseek", "thinking_enabled": False,
            "reasoning_effort": "low", "system_prompt_role": "user", "extra_body": {"max_tokens": 4096},
            "secrets": {"api_key": {"value": "summary-test-key"}},
        }]
        agents = [{
            "config_id": "eth-summary", "symbol": "ETH/USDT", "mode": "STRATEGY",
            "model": "trade-model", "system_prompt_role": "system",
            "summarizer_provider_id": "deepseek-summary",
            "strategy_prompt": "Compress to 80 characters: {content}",
            "summarizer": {"strategy_prompt": "Nested configured prompt: {content}"},
        }]
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), agents, providers, [])

        agent = config_store.load_runtime_snapshot()["agents"][0]
        summary = agent["summarizer"]
        self.assertEqual(summary["model"], "deepseek-flash")
        self.assertEqual(summary["api_key"], "summary-test-key")
        self.assertEqual(summary["api_base"], "https://summary.example.test/v1")
        self.assertEqual(summary["system_prompt_role"], "user")
        self.assertEqual(summary["extra_body"], {"max_tokens": 4096})
        self.assertEqual(summary["compatibility_mode"], "deepseek")
        self.assertFalse(summary["thinking_enabled"])
        self.assertEqual(summary["reasoning_effort"], "low")
        self.assertEqual(agent["strategy_prompt"], "Compress to 80 characters: {content}")

        # A task's explicit role override follows the same precedence as its
        # primary model role; unrelated provider defaults must not erase it.
        agents[0]["summarizer"]["system_prompt_role"] = "system"
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), agents, providers, [])
        self.assertEqual(config_store.load_runtime_snapshot()["agents"][0]["summarizer"]["system_prompt_role"], "system")

    def test_fallback_credentials_stay_in_provider_secret_storage_and_explicit_exports(self):
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [{
            'config_id': 'fallback-test', 'symbol': 'BTC/USDT', 'mode': 'STRATEGY',
            'fallback_models': [{'model': 'backup', 'api_key': 'sensitive-fallback-value', 'api_base': 'https://backup.test/v1'}],
        }])
        runtime = config_store.load_runtime_snapshot()
        self.assertEqual(runtime['agents'][0]['fallback_models'][0]['api_key'], 'sensitive-fallback-value')
        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute('SELECT data_json FROM agent_configs').fetchone()[0]
        self.assertNotIn('sensitive-fallback-value', stored)
        self.assertNotIn('sensitive-fallback-value', json.dumps(config_store.load_management_snapshot()))
        self.assertNotIn('sensitive-fallback-value', json.dumps(config_store.export_full_snapshot(False)))
        self.assertNotIn('sensitive-fallback-value', json.dumps(config_store.export_agent_configs()))
        exported = config_store.export_full_snapshot(True)
        backup = next(item for item in exported['llm_providers'] if item['model'] == 'backup')
        self.assertEqual(backup['_secrets']['api_key'], 'sensitive-fallback-value')
        self.assertTrue(exported['agents'][0]['fallback_llm_provider_ids'])

    def test_legacy_default_cadence_and_explicit_hourly_profile_survive_roundtrip(self):
        from backend.utils.run_schedule import effective_schedule
        from datetime import datetime
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [
            {'config_id': 'old-real', 'symbol': 'BTC/USDT', 'mode': 'REAL'},
            {'config_id': 'hourly', 'symbol': 'BTC/USDT', 'mode': 'REAL', 'market_profile': 'hourly'},
            {'config_id': 'explicit', 'symbol': 'BTC/USDT', 'mode': 'REAL', 'market_profile': 'hourly', 'run_interval': 30},
        ])
        agents = config_store.load_runtime_snapshot()['agents']
        intervals = {item['config_id']: effective_schedule(item, datetime(2026, 10, 8, 9))['interval'] for item in agents}
        self.assertEqual(intervals, {'old-real': 15, 'hourly': 60, 'explicit': 30})

    def test_invalid_imported_dca_schedule_cannot_replace_saved_tasks(self):
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [{'config_id': 'keep', 'symbol': 'BTC/USDT'}])
        with self.assertRaises(ValueError):
            config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [
                {'config_id': 'bad', 'symbol': 'BTC/USDT', 'mode': 'SPOT_DCA', 'dca_schedule': {'times': ['25:00']}},
            ])
        self.assertEqual(config_store.load_runtime_snapshot()['agents'][0]['config_id'], 'keep')

    def test_save_runtime_snapshot_preserves_agent_market_timeframes(self):
        config_store.save_runtime_snapshot(
            dict(config_store.DEFAULT_GLOBAL_SETTINGS),
            [
                {
                    "config_id": "btc-timeframes",
                    "symbol": "BTC/USDT",
                    "mode": "STRATEGY",
                    "model": "gpt-4o-mini",
                    "market_timeframes": ["15m", "1h", "1d"],
                }
            ],
        )

        snapshot = config_store.load_runtime_snapshot()
        self.assertEqual(snapshot["agents"][0]["market_timeframes"], ["15m", "1h", "1d"])

    def test_spot_portfolio_save_edit_export_import_roundtrip(self):
        from backend.app.schemas.payloads import ConfigAgentPayload
        from backend.config import config as runtime_config

        payload = ConfigAgentPayload(
            config_id="portfolio", mode="SPOT_DCA",
            symbols=[" btc/usdt ", "ETH/USDT", "BTC/USDT"],
            dca_amount=100, dca_budget=3000,
        ).model_dump()
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [payload])
        snapshot = config_store.load_runtime_snapshot()
        agent = snapshot["agents"][0]
        self.assertEqual(agent["symbols"], ["BTC/USDT", "ETH/USDT"])
        self.assertEqual(agent["symbol"], "BTC/USDT")
        self.assertEqual(agent["market_type"], "spot")
        self.assertEqual(agent["market_timeframes"], ["4h", "1d", "1w"])
        self.assertEqual(snapshot["exchange_profiles"][0]["market_type"], "spot")

        management = config_store.load_management_snapshot()
        management["agents"][0]["symbols"] = ["SOL/USDT", "ETH/USDT"]
        management["agents"][0]["market_timeframes"] = ["4h", "1d"]
        config_store.save_runtime_snapshot(management["globals"], management["agents"], management["llm_providers"], management["exchange_profiles"])
        exported = config_store.export_full_snapshot(include_secrets=False)
        with patch.object(runtime_config, "reload_config"):
            config_store.import_full_snapshot(exported)
        restored = config_store.load_runtime_snapshot()["agents"][0]
        self.assertEqual(restored["symbols"], ["SOL/USDT", "ETH/USDT"])
        self.assertEqual(restored["symbol"], "SOL/USDT")
        self.assertEqual(restored["market_timeframes"], ["4h", "1d"])
        self.assertEqual(restored["dca_amount"], 100)
        self.assertEqual(restored["dca_budget"], 3000)

    def test_spot_portfolio_invalid_inputs_do_not_replace_saved_config(self):
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [
            {"config_id": "keep", "mode": "SPOT_DCA", "symbol": "BTC/USDT"},
        ])
        invalid = [
            {"symbols": ["BTC/USDT", "ETH/USDC"]},
            {"symbols": ["BTC/USDT:USDT"]},
            {"symbols": ["BTC/USDT", "ETH/USDT"], "initial_qty": 1},
            {"symbols": ["BTC/USDT", "ETH/USDT"], "initial_cost": 100},
            {"symbols": [f"TOKEN{index}/USDT" for index in range(11)]},
            {"symbols": "BTC/USDT,ETH/USDT"},
            {"symbols": ["BTC/USDT", "ETH/USDT"], "mode": "REAL"},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [
                    {"config_id": "invalid", "mode": "SPOT_DCA", **overrides},
                ])
        self.assertEqual(config_store.load_runtime_snapshot()["agents"][0]["config_id"], "keep")

    def test_spot_portfolio_rejects_swap_profile(self):
        with self.assertRaisesRegex(ValueError, "requires a spot exchange profile"):
            config_store.save_runtime_snapshot(
                dict(config_store.DEFAULT_GLOBAL_SETTINGS),
                [{"config_id": "portfolio", "mode": "SPOT_DCA", "symbols": ["BTC/USDT", "ETH/USDT"], "exchange_profile_id": "swap"}],
                [], [{"profile_id": "swap", "name": "Swap", "exchange": "binance", "market_type": "swap"}],
            )

    def test_legacy_spot_import_gets_spot_profile_without_changing_shared_swap_profile(self):
        from backend.config import config as runtime_config

        data = {"version": 1, "agents": [
            {"config_id": "legacy-spot", "mode": "SPOT_DCA", "symbol": "BTC/USDT", "exchange_profile_id": "shared"},
            {"config_id": "futures", "mode": "REAL", "symbol": "BTC/USDT", "exchange_profile_id": "shared"},
        ], "exchange_profiles": [{"profile_id": "shared", "name": "Shared", "exchange": "binance", "market_type": "swap",
                                  "_secrets": {"api_key": "legacy-key", "secret": "legacy-secret"}}]}
        with patch.object(runtime_config, "reload_config"):
            config_store.import_full_snapshot(data)
        snapshot = config_store.load_runtime_snapshot()
        agents = {agent["config_id"]: agent for agent in snapshot["agents"]}
        profiles = {profile["profile_id"]: profile for profile in snapshot["exchange_profiles"]}
        self.assertEqual(agents["legacy-spot"]["market_type"], "spot")
        self.assertEqual(agents["legacy-spot"]["binance_api_key"], "legacy-key")
        self.assertNotEqual(agents["legacy-spot"]["exchange_profile_id"], "shared")
        self.assertEqual(profiles["shared"]["market_type"], "swap")
        self.assertEqual(agents["futures"]["exchange_profile_id"], "shared")

    def test_legacy_runtime_profile_upgrade_does_not_write_database(self):
        config_store.save_runtime_snapshot(dict(config_store.DEFAULT_GLOBAL_SETTINGS), [
            {"config_id": "legacy", "mode": "REAL", "symbol": "BTC/USDT", "exchange_profile_id": "shared"},
        ], [], [{"profile_id": "shared", "name": "Shared", "exchange": "binance", "market_type": "swap"}])
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT data_json FROM agent_configs WHERE config_id='legacy'").fetchone()
            payload = json.loads(row[0])
            payload['mode'] = 'SPOT_DCA'
            conn.execute("UPDATE agent_configs SET mode='SPOT_DCA',data_json=? WHERE config_id='legacy'", (json.dumps(payload),))
        snapshot = config_store.load_runtime_snapshot()
        self.assertEqual(snapshot['agents'][0]['market_type'], 'spot')
        self.assertNotEqual(snapshot['agents'][0]['exchange_profile_id'], 'shared')
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM exchange_profiles').fetchone()[0], 1)

    def test_legacy_spot_single_symbol_keeps_initial_holdings_and_explicit_timeframes(self):
        agent = config_store.normalize_agent_market_settings({
            "config_id": "legacy", "mode": "SPOT_DCA", "symbol": "ETH/USDT",
            "initial_qty": 2, "initial_cost": 4000, "market_timeframes": ["1h", "1d"],
        })
        self.assertEqual(agent["symbols"], ["ETH/USDT"])
        self.assertEqual(agent["initial_cost"], 4000)
        self.assertEqual(agent["market_timeframes"], ["1h", "1d"])
        self.assertEqual(agent["market_type"], "spot")

    def test_runtime_config_lookup_includes_secondary_spot_symbol(self):
        from backend.config import Config

        runtime = Config.__new__(Config)
        runtime.symbol_configs = runtime._normalize_symbol_configs([
            {"config_id": "portfolio", "mode": "SPOT_DCA", "symbols": ["BTC/USDT", "ETH/USDT"], "exchange": "okx", "okx_api_key": "fake-key", "okx_secret": "fake-secret", "passphrase": "fake-pass"},
        ])
        runtime.configs_by_id = {agent["config_id"]: agent for agent in runtime.symbol_configs}
        self.assertEqual(runtime.get_symbol_config("ETH/USDT")["config_id"], "portfolio")
        self.assertEqual(len(runtime.get_configs_by_symbol("ETH/USDT")), 1)
        self.assertEqual(runtime.get_exchange_credentials(symbol="ETH/USDT"), ("okx", "fake-key", "fake-secret", "fake-pass"))

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

    def test_env_snapshot_accepts_langsmith_aliases(self):
        with patch.dict(
            os.environ,
            {
                "LANGCHAIN_PROJECT": "",
                "LANGCHAIN_API_KEY": "",
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_PROJECT": "langsmith-project",
                "LANGSMITH_API_KEY": "langsmith-key",
            },
            clear=False,
        ):
            snapshot = config_store._env_snapshot()

        self.assertTrue(snapshot["langchain_tracing"])
        self.assertEqual(snapshot["langchain_project"], "langsmith-project")
        self.assertEqual(snapshot["langchain_api_key"], "langsmith-key")


if __name__ == "__main__":
    unittest.main()
