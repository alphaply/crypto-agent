"""Task-owned spot fills and weighted remaining cost, independent of cash flows."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from backend.database.spot_cost_basis import read_spot_order_totals


TERMINAL = {'FILLED', 'CLOSED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'REJECTED'}


def _time(value):
    if not value:
        return None
    from backend.database import TZ_CN
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (TZ_CN.localize(stamp) if stamp.tzinfo is None else stamp).astimezone(timezone.utc)


def spot_orders(conn, config_id: str, symbol: str, *, evidence_only=False) -> dict:
    return {**read_spot_order_totals(conn, config_id, symbol, side='buy', evidence_only=evidence_only),
            **read_spot_order_totals(conn, config_id, symbol, side='sell', evidence_only=evidence_only)}


def spot_fill_events(conn, config_id: str, symbol: str, orders: dict, basis=None, *, strict=True) -> list[dict]:
    rows = conn.execute('''SELECT * FROM trade_history t WHERE t.symbol=%s
        AND (t.config_id=%s OR t.config_id IS NULL OR t.config_id='')
        AND EXISTS (SELECT 1 FROM orders o WHERE o.config_id=%s AND o.symbol=t.symbol
          AND o.order_id=t.order_id AND o.trade_mode='SPOT_DCA'
          AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED')
        ORDER BY t.timestamp NULLS LAST,t.storage_id''', (symbol, config_id, config_id)).fetchall()
    by_order, seen = {}, set()
    # A legacy unassigned observation and its task-assigned copy are one fill.
    for row in sorted(rows, key=lambda r: r['config_id'] != config_id):
        identity = (row['symbol'], str(row['trade_id']))
        if identity in seen:
            continue
        seen.add(identity)
        by_order.setdefault(str(row['order_id']), []).append(dict(row))
    for fills in by_order.values():
        fills.sort(key=lambda r: (_time(r['timestamp']) or datetime.min.replace(tzinfo=timezone.utc), r['storage_id']))
    events = []
    baselines = basis['order_baselines'] if basis else {}
    for order_id, order in orders.items():
        previous = baselines.get(order_id)
        if previous and str(previous['status']).upper() in TERMINAL:
            continue
        # Old calibrations recorded only buy baselines. Do not turn old sells
        # into new sales; an unresolved old partial sell needs a fresh baseline.
        if basis and not previous and order['side'] == 'sell':
            stamp = _time(order.get('timestamp'))
            if stamp and stamp <= _time(basis['updated_at']):
                if order['status'] in TERMINAL:
                    continue
                raise ValueError('旧成本基准缺少卖出成交边界，请重新核对持仓成本')
        covered = float(previous['qty']) if previous else 0.0
        quantity = max(0.0, order['qty'] - covered)
        if quantity <= 0:
            continue
        cost = order['cost'] - (float(previous['cost']) if previous else 0.0) if order['cost'] is not None else None
        if cost is None and strict or cost is not None and (cost <= 0 or not math.isfinite(cost)):
            raise ValueError('校准后的新增成交成本尚未确认')
        remaining, used_cost, costs_known = quantity, 0.0, True
        for fill in by_order.get(order_id, []):
            amount = float(fill['amount'] or 0)
            if amount <= 0:
                continue
            skip = min(covered, amount)
            covered -= skip
            take = min(remaining, amount - skip)
            if take <= 0:
                continue
            ratio = take / amount
            fill_cost = float(fill['cost']) * ratio if fill['cost'] is not None else (0.0 if strict else None)
            fee = fill['fee']
            events.append(dict(order_id=order_id, trade_id=str(fill['trade_id']),
                               evidence_id=f"trade:{fill['trade_id']}", side=order['side'], quantity=take,
                               cost=fill_cost, timestamp=fill['timestamp'],
                               fee=float(fee) * ratio if fee is not None else None,
                               fee_currency=fill['fee_currency'], coarse=False))
            remaining -= take
            used_cost += fill_cost or 0.0
            costs_known &= fill_cost is not None
        if remaining > max(1e-12, quantity * 1e-9):
            if cost is not None and costs_known and cost - used_cost <= 0:
                raise ValueError('成交明细与订单累计成本不一致，收益暂不可计算')
            events.append(dict(order_id=order_id, trade_id=None, evidence_id=f'order:{order_id}',
                               side=order['side'], quantity=remaining,
                               cost=cost-used_cost if cost is not None and costs_known else None, timestamp=order.get('fill_timestamp'),
                               fee=None, fee_currency=None, coarse=True,
                               first_time=order.get('first_buy'), last_time=order.get('last_buy')))
    if strict and any(e['side'] == 'sell' for e in events):
        if any(not e['timestamp'] for e in events):
            raise ValueError('买卖成交时间不完整，无法确认剩余成本')
        if any(e.get('coarse') and e.get('first_time') != e.get('last_time') for e in events):
            raise ValueError('部分成交明细不足，无法确认买卖先后及剩余成本')
    return sorted(events, key=lambda e: (_time(e['timestamp']) or datetime.min.replace(tzinfo=timezone.utc), e['order_id']))


def apply_spot_fill(event: dict, quantity: float, cost: float, symbol: str) -> dict:
    """One weighted-cost step, shared by holdings and historical realized returns."""
    base, quote = symbol.split('/')[:2]
    amount, value = event['quantity'], event['cost']
    fee, currency = event.get('fee'), event.get('fee_currency')
    base_fee = fee if currency == base and fee is not None else 0.0
    quote_fee = fee if currency == quote and fee is not None else 0.0
    if event['side'] == 'buy':
        return dict(quantity=quantity + amount - base_fee, remaining_cost=cost + value + quote_fee,
                    realized_pnl=0.0, disposed_fraction=0.0)
    disposed = amount + base_fee
    if quantity <= 0 or disposed > quantity + max(1e-12, quantity * 1e-9):
        raise ValueError('卖出数量超过已确认持仓，剩余成本及收益暂不可计算')
    fraction = min(disposed / quantity, 1)
    removed = cost * fraction
    return dict(quantity=max(0.0, quantity - disposed), remaining_cost=max(0.0, cost - removed),
                realized_pnl=value - quote_fee - removed, disposed_fraction=fraction)


def value_spot_events(events: list[dict], quantity: float, cost: float, symbol: str) -> dict:
    base, quote = symbol.split('/')[:2]
    realized = buys = 0.0
    fees, missing_fees = {}, 0
    sales = 0
    for event in events:
        amount, value = event['quantity'], event['cost']
        fee, currency = event.get('fee'), event.get('fee_currency')
        if fee is None or not currency:
            missing_fees += 1
        else:
            fees[currency] = fees.get(currency, 0.0) + fee
        step = apply_spot_fill(event, quantity, cost, symbol)
        quantity, cost = step['quantity'], step['remaining_cost']
        realized += step['realized_pnl']
        if event['side'] == 'buy':
            buys += value
        else:
            sales += 1
    fees_complete = not missing_fees and not (set(fees) - {base, quote})
    return dict(quantity=quantity, remaining_cost=cost, subsequent_buy_cost=buys,
                realized_pnl=realized if fees_complete or not sales else None,
                realized_pnl_known_cost=realized, sell_fill_count=sales,
                fees_by_currency=fees, fees_complete=fees_complete, missing_fee_count=missing_fees)


def spot_holding_valuation(conn, config_id, symbol, orders, basis, initial_qty, initial_cost):
    quantity = basis['quantity'] if basis else initial_qty
    cost = quantity * basis['avg_cost'] if basis else initial_cost
    events = spot_fill_events(conn, config_id, symbol, orders, basis)
    result = value_spot_events(events, quantity, cost, symbol)
    if not basis:
        return result
    carry = basis.get('realized_carry')
    if carry is None:
        # Older baselines did not preserve realized outcomes. A known prior
        # sale cannot be silently replaced with zero after an upgrade.
        prior_sales = any(order['side'] == 'sell' and order['qty'] > 0 and (
            basis['order_baselines'].get(oid, {}).get('qty', 0) > 0 or
            _time(order.get('timestamp')) is not None and _time(order['timestamp']) <= _time(basis['updated_at'])
        ) for oid, order in orders.items())
        carry = {key: None if prior_sales else 0.0 for key in ('realized_pnl', 'realized_pnl_known_cost')}
    for key in ('realized_pnl', 'realized_pnl_known_cost'):
        result[key] = (result[key] + carry[key]) if result[key] is not None and carry.get(key) is not None else None
    return result
