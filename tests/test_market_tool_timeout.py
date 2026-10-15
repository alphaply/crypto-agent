import os
import unittest
from unittest.mock import patch

from backend.utils.market_data import MarketTool


class DummyExchange:
    urls = {'api': {'public': 'https://timeout.fixture.invalid'}}
    precisionMode, rateLimit = 4, 1
    def __init__(self, config):
        self.config = config
        self.options = dict(config['options'])
        self.id = 'binance' if self.options['defaultType'] == 'spot' else 'binanceusdm'
        self.timeout = config['timeout']
        self.markets = self.currencies = None

    def fetch(self, *args, **kwargs):
        raise AssertionError('Offline timeout fixture cannot make network calls')

    def load_markets(self):
        self.markets = {'BTC/USDT': {'symbol': 'BTC/USDT', 'spot': True, 'base': 'BTC', 'quote': 'USDT'}}
        self.currencies = {}
        return self.markets

    def set_markets(self, markets, currencies):
        self.markets, self.currencies = markets, currencies


class MarketToolTimeoutTests(unittest.TestCase):
    @patch("backend.utils.market_data.ccxt.binanceusdm", side_effect=lambda config: DummyExchange(config))
    @patch("backend.config.config.get_binance_credentials", return_value=("demo-key", "demo-secret"))
    @patch("backend.config.config.get_config_by_id", return_value={"symbol": "BTC/USDT", "mode": "REAL", "market_type": "swap"})
    def test_market_tool_sets_default_exchange_timeout(self, _mock_config, _mock_credentials, _mock_exchange):
        tool = MarketTool(config_id="cfg-a")
        self.addCleanup(tool.close)

        self.assertEqual(tool.exchange.config["timeout"], 15000)

    @patch("backend.utils.market_data.ccxt.binance", side_effect=lambda config: DummyExchange(config))
    @patch("backend.config.config.get_binance_credentials", return_value=("demo-key", "demo-secret"))
    @patch("backend.config.config.get_config_by_id", return_value={"symbol": "BTC/USDT", "mode": "SPOT_DCA", "market_type": "swap"})
    def test_market_tool_allows_env_override_for_exchange_timeout(self, _mock_config, _mock_credentials, _mock_exchange):
        with patch.dict(os.environ, {"EXCHANGE_TIMEOUT_MS": "7000"}, clear=False):
            tool = MarketTool(config_id="cfg-dca")
            self.addCleanup(tool.close)

        self.assertEqual(tool.exchange.config["timeout"], 7000)


if __name__ == "__main__":
    unittest.main()
