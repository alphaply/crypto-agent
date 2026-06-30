import unittest
from unittest.mock import patch

from backend.utils.market_data import MarketTool


class FakeExchange:
    def __init__(self, open_orders=None, positions=None, last_price=1500, cancel_responses=None):
        self.markets = {"BTC/USDT": {}}
        self.options = {"defaultType": "swap"}
        self.open_orders = list(open_orders or [])
        self.positions = list(positions or [])
        self.last_price = last_price
        self.cancel_responses = list(cancel_responses or [])
        self.cancelled = []
        self.created = []

    def amount_to_precision(self, _symbol, amount):
        return str(float(amount))

    def price_to_precision(self, _symbol, price):
        return str(float(price))

    def fetch_open_orders(self, _symbol, params=None):
        want_trigger = bool((params or {}).get("trigger"))
        return [
            order
            for order in self.open_orders
            if bool(order.get("is_trigger")) == want_trigger
        ]

    def cancel_order(self, order_id, _symbol, params=None):
        self.cancelled.append((order_id, params or {}))
        if self.cancel_responses:
            response = self.cancel_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return {"id": order_id, "status": "cancelled"}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        order = {
            "id": f"new-{len(self.created) + 1}",
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": price,
            "params": params or {},
        }
        self.created.append(order)
        return order

    def fetch_positions(self, _symbols):
        return self.positions

    def fetch_ticker(self, _symbol):
        return {"last": self.last_price}


class RealOrderMergeTests(unittest.TestCase):
    def make_tool(self, exchange):
        tool = MarketTool.__new__(MarketTool)
        tool.exchange = exchange
        return tool

    def test_cancel_order_falls_back_to_trigger_when_regular_reports_closed(self):
        exchange = FakeExchange(
            cancel_responses=[
                {"id": "stop-1", "status": "closed"},
                {"id": "stop-1", "status": "cancelled"},
            ]
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CANCEL",
            {"cancel_order_id": "stop-1"},
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(exchange.cancelled, [("stop-1", {}), ("stop-1", {"trigger": True})])

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_close_short_stop_order_merges_same_price_same_direction(self, _mock_db):
        exchange = FakeExchange(
            open_orders=[
                {
                    "id": "a",
                    "type": "STOP_MARKET",
                    "side": "buy",
                    "stopPrice": 1600,
                    "amount": 0.1,
                    "is_trigger": True,
                    "info": {"positionSide": "SHORT", "type": "STOP_MARKET"},
                }
            ],
            positions=[{"contracts": 1, "side": "short"}],
            last_price=1500,
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CLOSE",
            {"pos_side": "SHORT", "entry_price": 1600, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.cancelled, [("a", {"trigger": True})])
        self.assertEqual(exchange.created[0]["type"], "STOP_MARKET")
        self.assertEqual(exchange.created[0]["side"], "buy")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.3)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "SHORT")
        self.assertEqual(exchange.created[0]["params"]["stopPrice"], 1600.0)
        self.assertNotIn("closePosition", exchange.created[0]["params"])

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_close_short_full_stop_uses_close_position_without_quantity(self, _mock_db):
        exchange = FakeExchange(
            open_orders=[
                {
                    "id": "a",
                    "type": "STOP_MARKET",
                    "side": "buy",
                    "stopPrice": 1600,
                    "amount": 0.1,
                    "is_trigger": True,
                    "info": {"positionSide": "SHORT", "type": "STOP_MARKET"},
                }
            ],
            positions=[{"contracts": 0.3, "side": "short"}],
            last_price=1500,
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CLOSE",
            {"pos_side": "SHORT", "entry_price": 1600, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.cancelled, [("a", {"trigger": True})])
        self.assertEqual(exchange.created[0]["type"], "STOP_MARKET")
        self.assertEqual(exchange.created[0]["side"], "buy")
        self.assertIsNone(exchange.created[0]["amount"])
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "SHORT")
        self.assertEqual(exchange.created[0]["params"]["stopPrice"], 1600.0)
        self.assertTrue(exchange.created[0]["params"]["closePosition"])

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_open_limit_order_merges_same_price_same_direction(self, _mock_db):
        exchange = FakeExchange(
            open_orders=[
                {
                    "id": "a",
                    "type": "LIMIT",
                    "side": "buy",
                    "price": 1600,
                    "amount": 0.1,
                    "info": {"positionSide": "LONG", "type": "LIMIT"},
                }
            ]
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "BUY_LIMIT",
            {"entry_price": 1600, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.cancelled, [("a", {})])
        self.assertEqual(exchange.created[0]["type"], "LIMIT")
        self.assertEqual(exchange.created[0]["side"], "buy")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.3)
        self.assertEqual(exchange.created[0]["price"], 1600.0)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "LONG")

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_open_short_limit_uses_sell_and_short_position_side(self, _mock_db):
        exchange = FakeExchange()
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "SELL_LIMIT",
            {"entry_price": 1600, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.created[0]["type"], "LIMIT")
        self.assertEqual(exchange.created[0]["side"], "sell")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.2)
        self.assertEqual(exchange.created[0]["price"], 1600.0)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "SHORT")

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_close_long_take_profit_uses_sell_limit_long_position_side(self, _mock_db):
        exchange = FakeExchange(
            positions=[{"contracts": 1, "side": "long"}],
            last_price=1500,
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CLOSE",
            {"pos_side": "LONG", "entry_price": 1600, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.created[0]["type"], "LIMIT")
        self.assertEqual(exchange.created[0]["side"], "sell")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.2)
        self.assertEqual(exchange.created[0]["price"], 1600.0)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "LONG")

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_close_short_take_profit_uses_buy_limit_short_position_side(self, _mock_db):
        exchange = FakeExchange(
            positions=[{"contracts": 1, "side": "short"}],
            last_price=1500,
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CLOSE",
            {"pos_side": "SHORT", "entry_price": 1400, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.created[0]["type"], "LIMIT")
        self.assertEqual(exchange.created[0]["side"], "buy")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.2)
        self.assertEqual(exchange.created[0]["price"], 1400.0)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "SHORT")

    @patch("backend.utils.market_data.database.get_db_conn", side_effect=RuntimeError("no db"))
    def test_close_long_stop_uses_sell_stop_long_position_side(self, _mock_db):
        exchange = FakeExchange(
            positions=[{"contracts": 1, "side": "long"}],
            last_price=1500,
        )
        tool = self.make_tool(exchange)

        result = tool.place_real_order(
            "BTC/USDT",
            "CLOSE",
            {"pos_side": "LONG", "entry_price": 1400, "amount": 0.2},
        )

        self.assertEqual(result["id"], "new-1")
        self.assertEqual(exchange.created[0]["type"], "STOP_MARKET")
        self.assertEqual(exchange.created[0]["side"], "sell")
        self.assertAlmostEqual(exchange.created[0]["amount"], 0.2)
        self.assertEqual(exchange.created[0]["params"]["positionSide"], "LONG")
        self.assertEqual(exchange.created[0]["params"]["stopPrice"], 1400.0)
        self.assertNotIn("closePosition", exchange.created[0]["params"])


if __name__ == "__main__":
    unittest.main()
