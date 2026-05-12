from collections.abc import Callable
from contextlib import AbstractContextManager


def _normalize_trade_mode(trade_mode: str) -> str:
    if trade_mode == "REAL":
        return "REAL"
    if trade_mode == "SPOT_DCA":
        return "SPOT_DCA"
    return "STRATEGY"


class OrderPersistenceStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], timestamp_factory: Callable[[], str]):
        self._conn_factory = conn_factory
        self._timestamp_factory = timestamp_factory

    def save_order_log(self, order_id, symbol, agent_name, side, entry, tp, sl, reason, trade_mode="STRATEGY", config_id=None, amount=0, status="OPEN"):
        timestamp = self._timestamp_factory()
        valid_mode = _normalize_trade_mode(trade_mode)

        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO orders (order_id, timestamp, symbol, agent_name, config_id, side, entry_price, amount, take_profit, stop_loss, reason, trade_mode, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (str(order_id), timestamp, symbol, str(agent_name), config_id or str(agent_name), side, entry, amount, tp, sl, reason, valid_mode, status),
            )
            conn.commit()

    def update_fill_status(self, order_id, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE orders
                SET status = ?,
                    filled_amount = ?,
                    filled_cost = ?,
                    avg_fill_price = ?,
                    filled_at = ?
                WHERE order_id = ?
                ''',
                (status, float(filled_qty or 0), float(filled_cost or 0), float(avg_fill_price or 0), filled_at, str(order_id)),
            )
            conn.commit()

    def upsert_spot_fill(self, order_id, config_id, symbol, status, filled_qty=0.0, filled_cost=0.0, avg_fill_price=0.0, filled_at=None):
        last_sync_at = self._timestamp_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO spot_order_fills (order_id, config_id, symbol, status, filled_qty, filled_cost, avg_fill_price, filled_at, last_sync_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    config_id = excluded.config_id,
                    symbol = excluded.symbol,
                    status = excluded.status,
                    filled_qty = excluded.filled_qty,
                    filled_cost = excluded.filled_cost,
                    avg_fill_price = excluded.avg_fill_price,
                    filled_at = excluded.filled_at,
                    last_sync_at = excluded.last_sync_at
                ''',
                (
                    str(order_id),
                    str(config_id),
                    str(symbol),
                    str(status),
                    float(filled_qty or 0),
                    float(filled_cost or 0),
                    float(avg_fill_price or 0),
                    filled_at,
                    last_sync_at,
                ),
            )
            conn.commit()

    def get_paginated_orders(self, config_id, page=1, per_page=10):
        offset = (page - 1) * per_page
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM orders WHERE config_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (config_id, per_page, offset),
            )
            orders = [dict(row) for row in cursor.fetchall()]

            cursor.execute("SELECT COUNT(*) FROM orders WHERE config_id = ?", (config_id,))
            total = cursor.fetchone()[0]
            return orders, total