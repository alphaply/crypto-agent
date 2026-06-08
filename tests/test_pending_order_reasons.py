import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def create_orders_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            config_id TEXT,
            reason TEXT
        )
        """
    )
    conn.commit()
    conn.close()


class PendingOrderReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pending_reasons.db"
        create_orders_table(self.db_path)
        self.db_patch = patch.object(stats_service, "DB_NAME", str(self.db_path))
        self.db_patch.start()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_fetch_real_open_orders_backfills_reason_from_local_order_log(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO orders (order_id, config_id, reason) VALUES (?, ?, ?)",
                ("limit-1", "cfg-a", "entry retest"),
            )
            conn.execute(
                "INSERT INTO orders (order_id, config_id, reason) VALUES (?, ?, ?)",
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
