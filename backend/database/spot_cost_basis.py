"""Read spot buy evidence shared by cost calibration and dashboard statistics."""
from __future__ import annotations

import json
import math
from typing import Any

from backend import storage


def read_spot_unsettled_buy_ids(
    conn: storage.Connection, config_id: str, symbol: str, *, limit: int | None = None,
) -> list[str]:
    """Find buys whose order or cumulative-fill evidence is not terminal.

    Some execution paths commit the order before its fill snapshot. Both
    sources must agree that an order is terminal before calibration can freeze
    its historical fills without an exchange read.
    """
    row_limit = 'LIMIT %s' if limit is not None else ''
    parameters = (config_id, symbol, limit) if limit is not None else (config_id, symbol)
    rows = conn.execute(f'''SELECT o.order_id FROM orders o
        LEFT JOIN spot_order_fills f
          ON f.config_id=o.config_id AND f.symbol=o.symbol AND f.order_id=o.order_id
        WHERE o.config_id=%s AND o.symbol=%s AND o.trade_mode='SPOT_DCA'
          AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
          AND UPPER(o.side) IN ('BUY','BUY_LIMIT')
          AND (UPPER(COALESCE(o.status,''))
                NOT IN ('FILLED','CLOSED','CANCELLED','CANCELED','EXPIRED','REJECTED')
            OR (f.order_id IS NOT NULL AND UPPER(COALESCE(f.status,''))
                NOT IN ('FILLED','CLOSED','CANCELLED','CANCELED','EXPIRED','REJECTED')))
        GROUP BY o.order_id ORDER BY MAX(o.id) DESC {row_limit}''', parameters).fetchall()
    return [str(row['order_id']) for row in rows]


def read_spot_buy_totals(
    conn: storage.Connection, config_id: str, symbol: str,
) -> dict[str, dict[str, Any]]:
    """Return one cumulative fill per owned order, including unfilled orders.

    Exchange trade-history pagination must not remove old fills or count the
    same fill twice. Keep the existing dashboard precedence between order,
    synchronized fill, and owned trade-history evidence.
    """
    rows = conn.execute('''SELECT o.*,f.filled_qty AS sync_qty,f.filled_cost AS sync_cost,
            f.avg_fill_price AS sync_price,f.status AS sync_status,
            t.traded_qty,t.traded_cost,t.first_buy,t.last_buy
        FROM orders o LEFT JOIN spot_order_fills f
          ON f.config_id=o.config_id AND f.symbol=o.symbol AND f.order_id=o.order_id
        LEFT JOIN (SELECT order_id,SUM(amount) AS traded_qty,SUM(cost) AS traded_cost,
            MIN(timestamp) AS first_buy,MAX(timestamp) AS last_buy FROM trade_history
            WHERE symbol=%s AND (config_id=%s OR config_id IS NULL OR config_id='')
              AND LOWER(side)='buy' GROUP BY order_id) t ON t.order_id=o.order_id
        WHERE o.config_id=%s AND o.symbol=%s AND o.trade_mode='SPOT_DCA'
          AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
          AND UPPER(o.side) IN ('BUY','BUY_LIMIT') ORDER BY o.id DESC''',
        (symbol, config_id, config_id, symbol)).fetchall()
    totals = {}
    for row in rows:
        order_id = str(row['order_id'])
        if order_id in totals:
            continue
        synced = row['sync_status'] is not None
        qty = float((row['sync_qty'] if synced else row['filled_amount']) or 0)
        cost = float((row['sync_cost'] if synced else row['filled_cost']) or 0)
        avg = float((row['sync_price'] if synced else row['avg_fill_price']) or row['entry_price'] or 0)
        cost = max(cost, qty * avg)
        history_qty = float(row['traded_qty'] or 0)
        history_cost = float(row['traded_cost'] or 0)
        if history_qty >= qty:
            qty, cost = history_qty, max(cost, history_cost)
        if any(not math.isfinite(value) or value < 0 for value in (qty, cost)):
            raise ValueError('现货历史成交数量或成本无效')
        dates = [value for value in (
            row['first_buy'], row['last_buy'], row['filled_at'] or row['timestamp'],
        ) if value] if qty > 0 else []
        totals[order_id] = {
            'order_id': order_id, 'qty': qty, 'cost': cost,
            'status': str(row['sync_status'] if synced else row['status'] or 'UNKNOWN').upper(),
            'first_buy': min(dates) if dates else None,
            'last_buy': max(dates) if dates else None,
        }
    return totals


def read_spot_cost_basis(
    conn: storage.Connection, config_id: str, symbol: str,
) -> dict[str, Any] | None:
    row = conn.execute('''SELECT quantity,avg_cost,order_baselines_json,updated_at
        FROM spot_cost_basis WHERE config_id=%s AND symbol=%s''', (config_id, symbol)).fetchone()
    if row is None:
        return None
    return {
        'quantity': float(row['quantity']),
        'avg_cost': float(row['avg_cost']),
        'order_baselines': json.loads(row['order_baselines_json']),
        'updated_at': row['updated_at'],
    }


def buys_after_cost_basis(
    totals: dict[str, dict[str, Any]], order_baselines: dict[str, dict[str, Any]],
) -> tuple[float, float]:
    """Exclude covered history, but keep later fills on previously open orders."""
    quantity = cost = 0.0
    for order_id, order in totals.items():
        previous = order_baselines.get(order_id)
        if previous is None:
            if order['qty'] > 0 and order['cost'] <= 0:
                raise ValueError('校准后的新增成交成本尚未确认')
            quantity += order['qty']
            cost += order['cost']
        elif str(previous['status']).upper() in {'OPEN', 'PARTIAL', 'PARTIALLY_FILLED', 'UNKNOWN'}:
            added_qty = max(0.0, order['qty'] - float(previous['qty']))
            if added_qty > 0:
                added_cost = order['cost'] - float(previous['cost'])
                if added_cost <= 0:
                    raise ValueError('校准后的新增成交成本尚未确认')
                quantity += added_qty
                cost += added_cost
        # Terminal orders were already included in the user-entered holding.
        # A later history backfill must not add their old purchases again.
    return quantity, cost
