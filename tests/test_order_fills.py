import pytest
import unittest

import backend.database as database


class OrderFillTests(unittest.TestCase):


    def test_save_order_log_normalizes_trade_mode_and_config_fallback(self):
        database.save_order_log(
            order_id="ORD-1",
            symbol="BTC/USDT",
            agent_name="agent-a",
            side="buy",
            entry=100,
            tp=110,
            sl=90,
            reason="test",
            trade_mode="INVALID",
            config_id=None,
            amount=1.5,
            status="OPEN",
        )

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT config_id, trade_mode, amount, status FROM orders WHERE order_id = %s",
                ("ORD-1",),
            ).fetchone()

        self.assertEqual(row["config_id"], "agent-a")
        self.assertEqual(row["trade_mode"], "STRATEGY")
        self.assertEqual(row["amount"], 1.5)
        self.assertEqual(row["status"], "OPEN")

    def test_update_order_fill_status_updates_fill_fields(self):
        with database.get_db_conn() as conn:
            conn.execute(
                "INSERT INTO orders (order_id, status) VALUES (%s, %s)",
                ("ORD-2", "OPEN"),
            )
            conn.commit()

        database.update_order_fill_status("ORD-2", "PARTIAL", 0.3, 30, 100, "2026-05-12 10:00:00")

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT status, filled_amount, filled_cost, avg_fill_price, filled_at FROM orders WHERE order_id = %s",
                ("ORD-2",),
            ).fetchone()

        self.assertEqual(row["status"], "PARTIAL")
        self.assertEqual(row["filled_amount"], 0.3)
        self.assertEqual(row["filled_cost"], 30)
        self.assertEqual(row["avg_fill_price"], 100)
        self.assertEqual(row["filled_at"], "2026-05-12 10:00:00")

    def test_upsert_spot_order_fill_overwrites_existing_row(self):
        database.upsert_spot_order_fill("ORD-3", "cfg-a", "BTC/USDT", "OPEN", 0, 0, 0, None)
        database.upsert_spot_order_fill("ORD-3", "cfg-a", "BTC/USDT", "FILLED", 2, 210, 105, "2026-05-12 11:00:00")

        with database.get_db_conn() as conn:
            row = conn.execute(
                "SELECT status, filled_qty, filled_cost, avg_fill_price, filled_at, last_sync_at FROM spot_order_fills WHERE order_id = %s",
                ("ORD-3",),
            ).fetchone()

        self.assertEqual(row["status"], "FILLED")
        self.assertEqual(row["filled_qty"], 2)
        self.assertEqual(row["filled_cost"], 210)
        self.assertEqual(row["avg_fill_price"], 105)
        self.assertEqual(row["filled_at"], "2026-05-12 11:00:00")
        self.assertTrue(row["last_sync_at"])

    def test_spot_fill_id_collisions_keep_other_symbols_and_tasks(self):
        database.upsert_spot_order_fill('same-id', 'cfg-a', 'BTC/USDT', 'OPEN')
        database.upsert_spot_order_fill('same-id', 'cfg-a', 'ETH/USDT', 'FILLED', 2, 50, 25)
        database.upsert_spot_order_fill('same-id', 'cfg-b', 'BTC/USDT', 'PARTIAL', 1, 100, 100)
        database.upsert_spot_order_fill('same-id', 'cfg-a', 'BTC/USDT', 'CANCELLED')
        with database.get_db_conn() as conn:
            rows = conn.execute('SELECT config_id,symbol,status FROM spot_order_fills').fetchall()
        self.assertEqual({(row['config_id'], row['symbol']): row['status'] for row in rows}, {
            ('cfg-a', 'BTC/USDT'): 'CANCELLED', ('cfg-a', 'ETH/USDT'): 'FILLED',
            ('cfg-b', 'BTC/USDT'): 'PARTIAL',
        })


if __name__ == "__main__":
    unittest.main()


pytestmark = pytest.mark.usefixtures("pg")
