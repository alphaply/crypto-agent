import pytest
from backend import storage
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.database as database
from backend.app.services import dashboard_service


def create_stats_tables(db_path: Path) -> None:
    """The isolated PostgreSQL fixture installs the full production schema."""


class DashboardStatsAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "stats_aggregation.db"
        create_stats_tables(self.db_path)
        self.db_patch = patch.object(database, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_overview_metrics_only_count_visible_config_ids(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-a", "o-a", "cfg-a", "2026-05-12 10:00:00", "BTC/USDT", "sell", 100, 1, 100, 10),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-b", "o-b", "cfg-b", "2026-05-12 11:00:00", "BTC/USDT", "buy", 95, 1, 95, -4),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-old", "o-old", "cfg-old", "2026-05-12 12:00:00", "BTC/USDT", "sell", 120, 1, 120, 100),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, timestamp, symbol, agent_name, config_id, side, status, realized_pnl, close_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("m-a", "2026-05-12 13:00:00", "BTC/USDT", "cfg-a", "cfg-a", "BUY", "CLOSED", 5, "2026-05-12 13:30:00"),
            )
            conn.execute(
                "INSERT INTO mock_orders (order_id, timestamp, symbol, agent_name, config_id, side, status, realized_pnl, close_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("m-old", "2026-05-12 14:00:00", "BTC/USDT", "cfg-old", "cfg-old", "SELL", "CLOSED", -20, "2026-05-12 14:30:00"),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-eth", "o-eth", "cfg-eth", "2026-05-12 15:00:00", "ETH/USDT", "sell", 50, 1, 50, 99),
            )
            conn.commit()

        metrics = dashboard_service.build_symbol_overview_metrics(
            "BTC/USDT",
            [
                {"config_id": "cfg-a", "mode": "STRATEGY"},
                {"config_id": "cfg-b", "mode": "REAL"},
            ],
        )

        self.assertEqual(metrics["total_trades"], 3)
        self.assertAlmostEqual(metrics["total_pnl"], 11.0)
        self.assertAlmostEqual(metrics["win_rate"], 66.67, places=2)

    @patch("backend.config.config.get_config_by_id", return_value={"mode": "SPOT_DCA"})
    def test_config_stats_use_trade_history_when_config_id_is_present(self, _mock_get_config):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-dca", "o-dca", "cfg-dca", "2026-05-12 16:00:00", "BTC/USDT", "sell", 110, 1, 110, 8),
            )
            conn.commit()

        stats = database.get_history_pnl_stats("BTC/USDT", config_id="cfg-dca")

        self.assertEqual(stats["total_trades"], 1)
        self.assertAlmostEqual(stats["total_pnl"], 8.0)
        self.assertAlmostEqual(stats["win_rate"], 100.0)

    @patch("backend.config.config.get_config_by_id", return_value={"mode": "REAL"})
    def test_real_config_stats_use_position_history_with_symbol_suffix(self, _mock_get_config):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO position_history (config_id, symbol, position_key, side, status, source, closed_at, close_price, amount, realized_pnl, raw_json, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("cfg-real", "ETH/USDT:USDT", "pos-1", "LONG", "CLOSED", "exchange_trade", "2026-05-12 16:00:00", 2300, 1, 3.5, "{}", "2026-05-12 16:00:00"),
            )
            conn.execute(
                "INSERT INTO position_history (config_id, symbol, position_key, side, status, source, closed_at, close_price, amount, realized_pnl, raw_json, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("cfg-real", "ETH/USDT:USDT", "pos-2", "SHORT", "CLOSED", "exchange_trade", "2026-05-12 17:00:00", 2290, 1, -1.5, "{}", "2026-05-12 17:00:00"),
            )
            conn.commit()

        stats = database.get_history_pnl_stats("ETH/USDT", config_id="cfg-real")

        self.assertEqual(stats["total_trades"], 2)
        self.assertAlmostEqual(stats["total_pnl"], 2.0)
        self.assertAlmostEqual(stats["win_rate"], 50.0)

    @patch("backend.config.config.get_config_by_id", return_value={"mode": "SPOT_DCA"})
    def test_zero_pnl_spot_trades_still_count_total_trades(self, _mock_get_config):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-z1", "o-z1", "cfg-zero", "2026-05-12 16:00:00", "BTC/USDT", "buy", 100, 1, 100, 0),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-z2", "o-z2", "cfg-zero", "2026-05-12 17:00:00", "BTC/USDT", "buy", 101, 1, 101, 0),
            )
            conn.commit()

        stats = database.get_history_pnl_stats("BTC/USDT", config_id="cfg-zero")

        self.assertEqual(stats["total_trades"], 2)
        self.assertAlmostEqual(stats["total_pnl"], 0.0)
        self.assertIsNone(stats["win_rate"])

    @patch("backend.app.services.dashboard_service.get_mock_equity_history", return_value=[])
    @patch("backend.app.services.dashboard_service.get_mock_account", return_value={})
    @patch("backend.app.services.dashboard_service.get_active_agents", return_value=["cfg-a", "cfg-b"])
    @patch("backend.app.services.dashboard_service.get_summary_count", return_value=0)
    @patch("backend.app.services.dashboard_service.get_paginated_summaries", return_value=[])
    @patch.object(
        dashboard_service.global_config,
        "get_all_symbol_configs",
        return_value=[
            {"config_id": "cfg-a", "symbol": "BTC/USDT", "mode": "STRATEGY"},
            {"config_id": "cfg-b", "symbol": "BTC/USDT", "mode": "STRATEGY"},
        ],
    )
    def test_history_payload_all_only_uses_current_symbol_configs(
        self,
        _mock_configs,
        _mock_summaries,
        _mock_summary_count,
        _mock_active_agents,
        _mock_mock_account,
        _mock_mock_history,
    ):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-h1", "o-h1", "cfg-a", "2026-05-12 10:00:00", "BTC/USDT", "sell", 100, 1, 100, 6),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-h2", "o-h2", "cfg-b", "2026-05-12 11:00:00", "BTC/USDT", "buy", 98, 1, 98, -2),
            )
            conn.execute(
                "INSERT INTO trade_history (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, realized_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ("t-h-old", "o-h-old", "cfg-old", "2026-05-12 12:00:00", "BTC/USDT", "sell", 120, 1, 120, 50),
            )
            conn.commit()

        payload = dashboard_service.build_history_payload("BTC/USDT", agent_filter="ALL")

        self.assertEqual(payload["pnl_stats"]["total_trades"], 2)
        self.assertAlmostEqual(payload["pnl_stats"]["total_pnl"], 4.0)
        self.assertAlmostEqual(payload["pnl_stats"]["win_rate"], 50.0)


if __name__ == "__main__":
    unittest.main()

pytestmark = pytest.mark.usefixtures("pg")
