import os
import unittest
from unittest.mock import patch

from backend.utils.market_data import MarketTool


class DummyExchange:
    def __init__(self, config):
        self.config = config

    def load_markets(self):
        return {}


class MarketToolTimeoutTests(unittest.TestCase):
    @patch("backend.utils.market_data.ccxt.binanceusdm", side_effect=lambda config: DummyExchange(config))
    @patch("backend.config.config.get_binance_credentials", return_value=("demo-key", "demo-secret"))
    @patch("backend.config.config.get_config_by_id", return_value={"symbol": "BTC/USDT", "mode": "REAL", "market_type": "swap"})
    def test_market_tool_sets_default_exchange_timeout(self, _mock_config, _mock_credentials, _mock_exchange):
        tool = MarketTool(config_id="cfg-a")

        self.assertEqual(tool.exchange.config["timeout"], 15000)

    @patch("backend.utils.market_data.ccxt.binance", side_effect=lambda config: DummyExchange(config))
    @patch("backend.config.config.get_binance_credentials", return_value=("demo-key", "demo-secret"))
    @patch("backend.config.config.get_config_by_id", return_value={"symbol": "BTC/USDT", "mode": "SPOT_DCA", "market_type": "swap"})
    def test_market_tool_allows_env_override_for_exchange_timeout(self, _mock_config, _mock_credentials, _mock_exchange):
        with patch.dict(os.environ, {"EXCHANGE_TIMEOUT_MS": "7000"}, clear=False):
            tool = MarketTool(config_id="cfg-dca")

        self.assertEqual(tool.exchange.config["timeout"], 7000)


if __name__ == "__main__":
    unittest.main()