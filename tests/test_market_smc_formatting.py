from pathlib import Path
import unittest

import pandas as pd
from langchain_core.messages import SystemMessage

from backend.utils.formatters import format_market_data_to_text
from backend.utils.indicators import calculate_liquidity_sweep_ifvg, calculate_smc


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
        self.assertIn("internal_structure", smc)
        self.assertIn("swing_structure", smc)
        self.assertTrue(smc["events"])
        self.assertTrue(smc["fvg"])
        self.assertIn(smc["zone"]["name"], {"premium", "upper_value", "equilibrium", "lower_value", "discount"})

    def test_liquidity_sweep_ifvg_detects_sweep_and_inverse_gap(self):
        rows = [
            (100, 101, 99, 100),
            (101, 103, 100, 102),
            (102, 104, 101, 103),
            (103, 106, 102, 105),
            (104, 105, 103, 104),
            (103, 104, 102, 103),
            (102, 103, 101, 102),
            (101, 102, 98, 99),
            (99, 100, 97, 98),
            (98, 101, 96, 100),
            (100, 108, 99, 104),
            (104, 105, 98, 99),
            (99, 100, 94, 98),
            (98, 102, 97, 101),
            (101, 103, 100, 102),
        ]
        df = pd.DataFrame(
            [{"open": o, "high": h, "low": l, "close": c, "volume": 100 + idx} for idx, (o, h, l, c) in enumerate(rows)]
        )

        payload = calculate_liquidity_sweep_ifvg(df, pivot_size=2)

        self.assertTrue(payload["sweeps"])
        self.assertTrue(payload["latest_sweep"])
        self.assertIn("inverse_fvg", payload)

    def test_market_formatter_outputs_10_candles_ema100_smc_and_no_trend_labels(self):
        text = format_market_data_to_text(
            {
                "current_price": 2265.7,
                "atr_base": 8.358,
                "sentiment": {"funding_rate": -0.00002, "open_interest": 2100000, "24h_quote_vol": 10400000000},
                "technical_indicators": {
                    "1w": {"trend": {"status": "Bearish Neutral", "adx": 20.3, "di_plus": 11, "di_minus": 16}},
                    "1M": {"trend": {"status": "Strong Uptrend", "adx": 15.8, "di_plus": 20, "di_minus": 12}},
                    "15m": {
                        "trend": {"status": "Strong Uptrend", "strength": "Weak/Ranging", "adx": 24.3, "di_plus": 10.5, "di_minus": 32.3},
                        "atr": 8.358,
                        "volume_status": "High",
                        "ema": {"ema_20": 2283.3, "ema_50": 2285.5, "ema_100": 2284.2, "ema_200": 2281.8},
                        "rsi_analysis": {"rsi": 32.4, "divergence": "bearish divergence 馃敶"},
                        "macd": {"diff": -6.379, "hist": -3.044, "momentum": "hist down 鈿狅笍"},
                        "bollinger": {"up": 2308.4, "low": 2265.2, "width": 0.0189},
                        "recent_opens": list(range(1, 12)),
                        "recent_highs": list(range(2, 13)),
                        "recent_lows": list(range(0, 11)),
                        "recent_closes": [x + 0.5 for x in range(1, 12)],
                        "smc": {
                            "structure": {"scope": "internal", "type": "CHoCH", "bias": "bearish", "level": 2260},
                            "internal_structure": {"scope": "internal", "type": "CHoCH", "bias": "bearish", "level": 2260},
                            "swing_structure": {"scope": "swing", "type": "BOS", "bias": "bullish", "level": 2290},
                            "events": [{"scope": "internal", "type": "CHoCH", "bias": "bearish", "level": 2260}],
                            "order_blocks": [{"bias": "bearish", "low": 2280, "high": 2290}],
                            "fvg": [{"bias": "bearish", "low": 2250, "high": 2260, "mitigated": False, "consumed_pct": 0}],
                            "liquidity": {"swing_high": 2300, "swing_low": 2200, "eqh": [2295], "eql": []},
                            "zone": {"name": "discount", "low": 2200, "high": 2300},
                        },
                        "liquidity_sweep_ifvg": {
                            "latest_sweep": {"direction": "bearish", "level": 2295},
                            "sweeps": [{"direction": "bearish", "level": 2295}],
                            "active_fvg": [{"bias": "bearish", "low": 2250, "high": 2260}],
                            "inverse_fvg": [{"bias": "bullish", "low": 2230, "high": 2240}],
                        },
                    },
                },
            }
        )

        self.assertIn("SMC:", text)
        self.assertIn("Liquidity/IFVG:", text)
        self.assertIn("100=2284.2", text)
        self.assertIn("Recent 10 candles", text)
        self.assertIn("[2,3,1,2.5]", text)
        self.assertNotIn("[1,2,0,1.5]", text)
        for removed_label in ["上涨排列", "下跌排列", "震荡偏多", "震荡偏空", "区间震荡", "Strong Uptrend", "Bearish Neutral"]:
            self.assertNotIn(removed_label, text)
        self.assertNotIn("鈿狅笍", text)
        self.assertNotIn("馃敶", text)
        self.assertNotIn("VP:", text)

    def test_agent_prompt_source_uses_system_message_for_rendered_prompt(self):
        source = Path("backend/agent/agent_graph.py").read_text(encoding="utf-8")

        self.assertIn("messages = [SystemMessage(content=system_prompt)]", source)
        self.assertNotIn("messages = [HumanMessage(content=system_prompt)]", source)
        self.assertEqual(SystemMessage(content="prompt").type, "system")


if __name__ == "__main__":
    unittest.main()
