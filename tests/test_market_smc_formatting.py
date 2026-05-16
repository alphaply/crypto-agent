import unittest

import pandas as pd

from backend.utils.formatters import format_market_data_to_text
from backend.utils.indicators import calculate_smc


class MarketSmcFormattingTests(unittest.TestCase):
    def test_calculate_smc_detects_structure_fvg_and_zone(self):
        closes = [100, 98, 96, 99, 101, 103, 100, 98, 97, 99, 102, 105, 107, 104, 101, 99, 96, 94, 97, 100, 103, 108]
        df = pd.DataFrame(
            [
                {"open": close - 0.4, "high": close + 1, "low": close - 1, "close": close, "volume": 100 + idx}
                for idx, close in enumerate(closes)
            ]
        )

        smc = calculate_smc(df, swing_length=4, internal_length=2)

        self.assertEqual(smc["structure"]["type"], "BOS")
        self.assertEqual(smc["structure"]["bias"], "bullish")
        self.assertTrue(smc["fvg"])
        self.assertIn(smc["zone"]["name"], {"premium", "upper_value", "equilibrium", "lower_value", "discount"})

    def test_market_formatter_cleans_labels_and_outputs_smc(self):
        text = format_market_data_to_text(
            {
                "current_price": 2265.7,
                "atr_base": 8.358,
                "sentiment": {"funding_rate": -0.00002, "open_interest": 2100000, "24h_quote_vol": 10400000000},
                "technical_indicators": {
                    "1w": {"trend": {"status": "Bearish Neutral", "adx": 20.3}},
                    "1M": {"trend": {"status": "Strong Uptrend", "adx": 15.8}},
                    "15m": {
                        "trend": {"status": "Strong Uptrend", "strength": "Weak/Ranging", "adx": 24.3, "di_plus": 10.5, "di_minus": 32.3},
                        "atr": 8.358,
                        "volume_status": "High",
                        "ema": {"ema_20": 2283.3, "ema_50": 2285.5, "ema_200": 2281.8},
                        "rsi_analysis": {"rsi": 32.4, "divergence": "看跌背离 🔴"},
                        "macd": {"diff": -6.379, "hist": -3.044, "momentum": "空头减速 ⚠️"},
                        "bollinger": {"up": 2308.4, "low": 2265.2, "width": 0.0189},
                        "recent_opens": [1, 2, 3, 4, 5, 6],
                        "recent_highs": [2, 3, 4, 5, 6, 7],
                        "recent_lows": [0, 1, 2, 3, 4, 5],
                        "recent_closes": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
                        "smc": {
                            "structure": {"type": "CHoCH", "bias": "bearish", "level": 2260},
                            "order_blocks": [{"bias": "bearish", "low": 2280, "high": 2290}],
                            "fvg": [{"bias": "bearish", "low": 2250, "high": 2260}],
                            "liquidity": {"swing_high": 2300, "swing_low": 2200, "eqh": [2295], "eql": []},
                            "zone": {"name": "discount", "low": 2200, "high": 2300},
                        },
                    },
                },
            }
        )

        self.assertIn("SMC:", text)
        self.assertIn("上涨排列", text)
        self.assertNotIn("Strong Uptrend", text)
        self.assertNotIn("空头减速", text)
        self.assertNotIn("⚠️", text)
        self.assertNotIn("🔴", text)
        self.assertNotIn("VP:", text)
        self.assertIn("近5根K线", text)


if __name__ == "__main__":
    unittest.main()
