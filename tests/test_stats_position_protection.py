import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("RUN_SCHEDULER_IN_WEB", "false")

from fastapi.testclient import TestClient

import backend.database as database
from backend.app.core.security import get_expected_password
from backend.app.main import app
from backend.app.services.stats_service import update_position_protection_payload
from backend.config import config as global_config


def setup_mock_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mock_orders (
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS real_protection_plans (
            config_id TEXT,
            symbol TEXT,
            side TEXT,
            payload TEXT,
            PRIMARY KEY (config_id, symbol, side)
        )
        """
    )
    conn.commit()
    conn.close()


class TestPositionProtectionService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_trading.db"
        setup_mock_db(self.db_path)
        self.patcher_db = patch.object(database, "DB_NAME", str(self.db_path))
        self.patcher_db.start()

    def tearDown(self):
        self.patcher_db.stop()
        self.temp_dir.cleanup()

    def test_update_protection_config_not_found(self):
        with patch.object(global_config, "get_config_by_id", return_value=None):
            with self.assertRaises(FileNotFoundError):
                update_position_protection_payload(
                    config_id="nonexistent",
                    symbol="ETH/USDT:USDT",
                    side="LONG",
                    stop_loss=2400.0,
                    take_profit=2600.0,
                )

    def test_update_protection_invalid_side(self):
        with patch.object(global_config, "get_config_by_id", return_value={"config_id": "cfg1", "mode": "STRATEGY"}):
            with self.assertRaises(ValueError):
                update_position_protection_payload(
                    config_id="cfg1",
                    symbol="ETH/USDT:USDT",
                    side="INVALID",
                    stop_loss=2400.0,
                )

    def test_update_protection_strategy_mode(self):
        # Insert a mock open position
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO mock_orders (order_id, symbol, config_id, side, price, amount, stop_loss, take_profit, status, is_filled)
            VALUES ('m1', 'ETH/USDT:USDT', 'cfg1', 'BUY', 2500.0, 0.5, 2450.0, 2600.0, 'OPEN', 1)
            """
        )
        conn.commit()
        conn.close()

        with patch.object(global_config, "get_config_by_id", return_value={"config_id": "cfg1", "mode": "STRATEGY"}):
            # Adjust SL and TP
            res = update_position_protection_payload(
                config_id="cfg1",
                symbol="ETH/USDT:USDT",
                side="LONG",
                stop_loss=2480.0,
                take_profit=2650.0,
            )
            self.assertEqual(res["stop_loss"], 2480.0)
            self.assertEqual(res["take_profit"], 2650.0)

            # Verify DB was updated
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT stop_loss, take_profit FROM mock_orders WHERE order_id='m1'").fetchone()
            conn.close()
            self.assertEqual(row[0], 2480.0)
            self.assertEqual(row[1], 2650.0)

            # Clear SL and TP
            res = update_position_protection_payload(
                config_id="cfg1",
                symbol="ETH/USDT:USDT",
                side="LONG",
                clear_stop_loss=True,
                clear_take_profit=True,
            )
            self.assertIsNone(res["stop_loss"])
            self.assertIsNone(res["take_profit"])

            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT stop_loss, take_profit FROM mock_orders WHERE order_id='m1'").fetchone()
            conn.close()
            self.assertIsNone(row[0])
            self.assertIsNone(row[1])

    def test_update_protection_real_mode(self):
        with patch.object(global_config, "get_config_by_id", return_value={"config_id": "cfg_real", "mode": "REAL"}):
            mock_protection = MagicMock()
            mock_protection.adjust.return_value = {
                "symbol": "ETH/USDT:USDT",
                "side": "SHORT",
                "stop_loss": 2550.0,
                "take_profit": 2400.0,
                "state": "ACTIVE",
                "error": None,
            }
            with patch("backend.utils.position_protection.PositionProtection", return_value=mock_protection):
                with patch("backend.utils.market_data.MarketTool", return_value=MagicMock()):
                    res = update_position_protection_payload(
                        config_id="cfg_real",
                        symbol="ETH/USDT:USDT",
                        side="SHORT",
                        stop_loss=2550.0,
                        take_profit=2400.0,
                        clear_stop_loss=False,
                        clear_take_profit=False,
                    )
                    self.assertEqual(res["stop_loss"], 2550.0)
                    self.assertEqual(res["take_profit"], 2400.0)
                    self.assertEqual(res["state"], "ACTIVE")
                    mock_protection.adjust.assert_called_once_with(
                        symbol="ETH/USDT:USDT",
                        side="SHORT",
                        sl=2550.0,
                        tp=2400.0,
                        clear_sl=False,
                        clear_tp=False,
                    )


class TestPositionProtectionApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_protection_api_endpoint(self):
        password = get_expected_password()
        login = self.client.post("/api/auth/login", json={"password": password})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        with patch("backend.app.api.stats.update_position_protection_payload") as mock_update:
            mock_update.return_value = {
                "config_id": "cfg1",
                "symbol": "ETH/USDT:USDT",
                "side": "LONG",
                "stop_loss": 2450.0,
                "take_profit": None,
                "state": "ACTIVE",
                "error": None,
            }

            resp = self.client.post(
                "/api/stats/position/protection",
                headers=headers,
                json={
                    "config_id": "cfg1",
                    "symbol": "ETH/USDT:USDT",
                    "side": "LONG",
                    "stop_loss": 2450.0,
                    "clear_take_profit": True,
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["stop_loss"], 2450.0)
            self.assertIsNone(data["take_profit"])


if __name__ == "__main__":
    unittest.main()
