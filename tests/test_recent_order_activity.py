import pytest
import unittest

import backend.database as database
from backend.app.services import dashboard_service


class RecentOrderActivityTests(unittest.TestCase):


    def test_recent_activity_collapses_duplicate_cancel_logs(self):
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "BUY",
            100,
            110,
            90,
            "open reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            amount=0.2,
            status="OPEN",
        )
        with database.get_db_conn() as conn:
            conn.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s", ("ST-1",))
            conn.commit()
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "CANCEL_BUY",
            0,
            0,
            0,
            "cancel reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            status="CANCELLED",
        )
        database.save_order_log(
            "ST-1",
            "BTC/USDT",
            "cfg-a",
            "CANCEL",
            0,
            0,
            0,
            "duplicate cancel reason",
            trade_mode="STRATEGY",
            config_id="cfg-a",
            status="CANCELLED",
        )

        payload = dashboard_service.get_recent_order_activity_payload("cfg-a", limit=10)

        self.assertEqual(payload["total"], 1)
        row = payload["orders"][0]
        self.assertEqual(row["order_id"], "ST-1")
        self.assertEqual(row["action_label"], "撤单")
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(row["entry_price"], 100)
        self.assertEqual(row["amount"], 0.2)
        self.assertEqual(row["take_profit"], 110)
        self.assertEqual(row["stop_loss"], 90)
        self.assertEqual(row["reason"], "duplicate cancel reason")


if __name__ == "__main__":
    unittest.main()


pytestmark = pytest.mark.usefixtures("pg")
