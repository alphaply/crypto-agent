import pytest
from backend import storage
import unittest
from contextlib import closing

from backend.app.services import stats_service


class FakeExchange:
    def fetch_open_orders(self, _symbol, params=None):
        if params and params.get("trigger"):
            return [
                {
                    "id": "stop-1",
                    "type": "STOP_MARKET",
                    "side": "sell",
                    "stopPrice": 1400,
                    "amount": 0.2,
                    "info": {"positionSide": "LONG", "type": "STOP_MARKET"},
                }
            ]
        return [
            {
                "id": "limit-1",
                "type": "LIMIT",
                "side": "buy",
                "price": 1500,
                "amount": 0.1,
                "info": {"positionSide": "LONG", "type": "LIMIT"},
            }
        ]


class FakeMarketTool:
    exchange = FakeExchange()


class PendingOrderReasonTests(unittest.TestCase):


    def test_fetch_real_open_orders_backfills_reason_from_local_order_log(self):
        with closing(storage.connect()) as conn:
            conn.execute(
                "INSERT INTO orders (order_id, config_id, reason) VALUES (%s, %s, %s)",
                ("limit-1", "cfg-a", "entry retest"),
            )
            conn.execute(
                "INSERT INTO orders (order_id, config_id, reason) VALUES (%s, %s, %s)",
                ("stop-1", "cfg-a", "risk invalidation"),
            )
            conn.commit()

        rows = stats_service._fetch_real_open_orders(
            FakeMarketTool(),
            "BTC/USDT",
            current_side="LONG",
            positions=[{"side": "LONG", "amount": 0.2}],
            config_id="cfg-a",
        )

        reason_by_id = {row["order_id"]: row["reason"] for row in rows}
        self.assertEqual(reason_by_id["limit-1"], "entry retest")
        self.assertEqual(reason_by_id["stop-1"], "risk invalidation")


if __name__ == "__main__":
    unittest.main()


pytestmark = pytest.mark.usefixtures("pg")
