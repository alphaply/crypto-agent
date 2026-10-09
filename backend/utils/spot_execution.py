"""Task-wide spot spending reservations and ownership checks.

Reservations are committed before exchange writes. An ambiguous write remains
reserved and blocks further buys until reconciled; changing symbols or tool IDs
must never manufacture more budget.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta
import math
import time
import hashlib

from backend import database
from backend.utils.spot_portfolio import get_config_symbols, normalize_spot_symbols


current_spot_cycle_id: ContextVar[str | None] = ContextVar("spot_cycle_id", default=None)


def resolve_spot_symbol(config: dict, requested: str | None = None) -> str:
    allowed = get_config_symbols(config)
    if not allowed:
        raise ValueError("现货任务没有配置可交易标的")
    symbol = normalize_spot_symbols([requested] if requested is not None else [allowed[0]])[0]
    if symbol not in allowed:
        raise ValueError(f"现货标的 {symbol} 不在本任务配置范围内")
    return symbol


def _initialize(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS spot_budget_reservations (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, cycle_id TEXT NOT NULL,
        symbol TEXT NOT NULL, quote_cost REAL NOT NULL, status TEXT NOT NULL,
        order_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(config_id, operation_id))""")
    columns = {row[1] for row in conn.execute('PRAGMA table_info(spot_budget_reservations)')}
    for name, column_type in (('client_order_id', 'TEXT'), ('entry_price', 'REAL'), ('amount', 'REAL')):
        if name not in columns:
            conn.execute(f'ALTER TABLE spot_budget_reservations ADD COLUMN {name} {column_type}')
    conn.execute('''CREATE TABLE IF NOT EXISTS spot_budget_cycles (
        config_id TEXT NOT NULL, cycle_id TEXT NOT NULL, origin TEXT NOT NULL,
        budget REAL NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(config_id,cycle_id))''')


def ensure_spot_budget_cycle(config_id: str, config: dict, cycle_id: str, origin: str = 'manual') -> str:
    """A logical run gets one persisted allowance; replay never creates another."""
    if not cycle_id or not str(cycle_id).strip():
        raise ValueError('现货交易需要有效运行周期，请重新创建交易运行')
    amount = config.get('dca_amount')
    if amount in (None, ''):
        amount = config.get('dca_budget')
    amount = _positive(100 if amount in (None, '') else amount, '每次运行组合预算')
    with database.get_db_conn() as conn:
        _initialize(conn)
        conn.execute('INSERT OR IGNORE INTO spot_budget_cycles VALUES (?,?,?,?,?)',
                     (config_id, str(cycle_id), origin, amount, time.time()))
        conn.commit()
    return str(cycle_id)


def _cycle_commitments(conn, config_id, cycle_id, reservations):
    rows = [row for row in reservations if row['cycle_id'] == cycle_id and row['status'] != 'released']
    # Fill reconciliation changes cost but never transfers an order into another run.
    total = 0.0
    seen = set()
    for row in rows:
        key = (row['symbol'], str(row['order_id']))
        if row['order_id'] and key in seen:
            continue
        if row['order_id']:
            seen.add(key)
        fill = conn.execute('SELECT * FROM spot_order_fills WHERE config_id=? AND symbol=? AND order_id=?',
                            (config_id, row['symbol'], row['order_id'])).fetchone() if row['order_id'] else None
        cost = float(row['quote_cost'])
        if fill:
            filled_cost = max(float(fill['filled_cost'] or 0), float(fill['filled_qty'] or 0) * float(fill['avg_fill_price'] or 0))
            if str(fill['status']).upper() in {'CANCELLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'FILLED', 'CLOSED'}:
                cost = filled_cost
            else:
                cost = filled_cost + max(0, float(row['amount'] or 0) - float(fill['filled_qty'] or 0)) * float(row['entry_price'] or 0)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError('现货成本无效，需核对后再买入')
        total += cost
    return total


def _positive(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} 必须为有限正数")
    return number


def _nonnegative(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} 必须为有限非负数")
    return number


def spot_budget_period(config: dict, now: datetime | None = None) -> tuple[str, float]:
    """A calendar day / Monday-based week in the scheduler's China timezone.

    Graph UUIDs identify executions, never an allowance. Counting timestamps also
    includes reservations created by older releases that stored UUID cycle IDs.
    """
    from backend.utils.run_schedule import normalize_dca_freq
    local = (now or datetime.now(database.TZ_CN)).astimezone(database.TZ_CN)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    weekly = normalize_dca_freq(config.get('dca_freq')) == '1w'
    if weekly:
        start -= timedelta(days=start.weekday())
    return f"{'week' if weekly else 'day'}:{start.date().isoformat()}", start.timestamp()


def spot_client_order_id(config_id: str, operation_id: str) -> str:
    return 'sp' + hashlib.sha256(f'{config_id}:{operation_id}'.encode()).hexdigest()[:28]


def _known_commitments(conn, config_id: str, created_after: float | None = None) -> tuple[float, set[tuple[str, str]]]:
    rows = conn.execute("""SELECT o.*, f.status AS sync_status,
        f.filled_qty AS sync_qty, f.filled_cost AS sync_cost,
        f.avg_fill_price AS sync_price
        FROM orders o LEFT JOIN spot_order_fills f
        ON f.order_id=o.order_id AND f.config_id=o.config_id AND f.symbol=o.symbol
        WHERE o.config_id=? AND o.trade_mode='SPOT_DCA'
        AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
        AND UPPER(o.side) IN ('BUY', 'BUY_LIMIT') ORDER BY o.id DESC""", (config_id,)).fetchall()
    seen = set()
    total = 0.0
    for row in rows:
        key = (row['symbol'], str(row['order_id']))
        if key in seen:
            continue
        seen.add(key)
        if created_after is not None:
            try:
                created = datetime.fromisoformat(str(row['timestamp']))
                if created.tzinfo is None:
                    created = database.TZ_CN.localize(created)
                if created.timestamp() < created_after:
                    continue
            except (ValueError, TypeError):
                # Missing timestamps must not manufacture a fresh allowance.
                pass
        synced = row['sync_status'] is not None
        status = str(row['sync_status'] if synced else row['status'] or '').upper()
        qty = float((row['sync_qty'] if synced else row['filled_amount']) or 0)
        cost = float((row['sync_cost'] if synced else row['filled_cost']) or 0)
        price = float(row['entry_price'] or 0)
        amount = float(row['amount'] or 0)
        avg = float((row['sync_price'] if synced else row['avg_fill_price']) or price)
        values = (qty, cost, price, amount, avg)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("现货历史订单成本数据无效，需核对后再买入")
        cost = max(cost, qty * avg)
        if status in {'FILLED', 'CLOSED'}:
            cost = cost or amount * price
        elif status not in {'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}:
            cost += max(0.0, amount - qty) * price
        elif not synced and qty == 0 and cost == 0:
            # Legacy cancellation logs alone cannot prove an order never filled.
            cost = amount * price
        total += cost
    return total, seen


def _reconcile_known_reservations(conn, config_id: str) -> None:
    # Only a known exchange ID, this task's creation log and synchronized fill
    # evidence may resolve an uncertain write. Missing IDs never expire away.
    rows = conn.execute("""SELECT DISTINCT r.operation_id, f.status,
        f.filled_qty, f.filled_cost, f.avg_fill_price
        FROM spot_budget_reservations r JOIN orders o
        ON o.config_id=r.config_id AND o.symbol=r.symbol AND o.order_id=r.order_id
        JOIN spot_order_fills f
        ON f.config_id=r.config_id AND f.symbol=r.symbol AND f.order_id=r.order_id
        WHERE r.config_id=? AND r.status IN ('reserved','unknown')
        AND o.trade_mode='SPOT_DCA' AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
        AND UPPER(o.side) IN ('BUY','BUY_LIMIT')""", (config_id,)).fetchall()
    for row in rows:
        if str(row['status']).upper() not in {'OPEN', 'PARTIAL', 'PARTIALLY_FILLED', 'FILLED', 'CLOSED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}:
            continue
        values = [float(row[key] or 0) for key in ('filled_qty', 'filled_cost', 'avg_fill_price')]
        if any(not math.isfinite(value) or value < 0 for value in values):
            continue
        if values[0] > 0 and values[1] <= 0 and values[2] <= 0:
            continue
        conn.execute("UPDATE spot_budget_reservations SET status='submitted',updated_at=? "
                     'WHERE config_id=? AND operation_id=?', (time.time(), config_id, row['operation_id']))


def reserve_spot_batch(config_id: str, config: dict, parent_id: str, orders: list) -> None:
    cycle_id = current_spot_cycle_id.get()
    if not cycle_id:
        raise ValueError('现货交易缺少运行周期，禁止通过工具调用创建新额度')
    per_cycle = config.get('dca_amount')
    if per_cycle in (None, ''):
        per_cycle = config.get('dca_budget')
    per_cycle = _positive(100 if per_cycle in (None, '') else per_cycle, '本轮总预算')
    lifetime = config.get('dca_budget')
    lifetime = _positive(lifetime, '任务总预算') if lifetime not in (None, '') else None
    costs = [_positive(order.entry_price * order.amount, '订单金额') for order in orders]
    requested = sum(costs)
    if not math.isfinite(requested):
        raise ValueError('订单总金额必须为有限正数')
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _initialize(conn)
        cycle = conn.execute('SELECT budget FROM spot_budget_cycles WHERE config_id=? AND cycle_id=?',
                             (config_id, cycle_id)).fetchone()
        if not cycle:
            raise ValueError('现货运行周期未注册或旧审批已过期，请重新分析')
        per_cycle = min(per_cycle, float(cycle['budget']))
        _reconcile_known_reservations(conn, config_id)
        reservations = conn.execute('SELECT * FROM spot_budget_reservations WHERE config_id=?', (config_id,)).fetchall()
        if any(row['status'] in {'reserved', 'unknown'} for row in reservations):
            raise ValueError('之前的现货下单结果未知或正在提交，待核验后才能继续买入')
        spent = _cycle_commitments(conn, config_id, cycle_id, reservations)
        if spent + requested > per_cycle + 1e-8:
            raise ValueError(f'所有标的共享本轮预算 {per_cycle:g}，本轮剩余 {max(0, per_cycle-spent):g}，申请 {requested:g}')
        if lifetime is not None:
            committed, known = _known_commitments(conn, config_id)
            committed += sum(float(row['quote_cost']) for row in reservations
                             if row['status'] != 'released' and (row['symbol'], str(row['order_id'])) not in known)
            if committed + requested > lifetime + 1e-8:
                raise ValueError(f'所有标的共享任务总预算 {lifetime:g}，已成交及挂单占用 {committed:g}，申请 {requested:g}')
        now = time.time()
        conn.executemany('''INSERT INTO spot_budget_reservations
            (config_id,operation_id,cycle_id,symbol,quote_cost,status,order_id,created_at,updated_at,
             client_order_id,entry_price,amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', [
            (config_id, f'{parent_id}:{index}', cycle_id, order.symbol, cost, 'reserved', None, now, now,
             spot_client_order_id(config_id, f'{parent_id}:{index}'), order.entry_price, order.amount)
            for index, (order, cost) in enumerate(zip(orders, costs))
        ])
        conn.commit()


def spot_budget_status(config_id: str, config: dict, cycle_id: str | None = None) -> dict:
    """The same durable allowance calculation used by order validation."""
    cycle_id = cycle_id or current_spot_cycle_id.get() or 'unallocated'
    limit = config.get('dca_amount')
    if limit in (None, ''):
        limit = config.get('dca_budget')
    limit = _nonnegative(100 if limit in (None, '') else limit, '周期预算')
    lifetime_limit = config.get('dca_budget')
    lifetime_limit = _nonnegative(lifetime_limit, '任务总预算') if lifetime_limit not in (None, '') else None
    with database.get_db_conn() as conn:
        _initialize(conn)
        cycle = conn.execute('SELECT budget FROM spot_budget_cycles WHERE config_id=? AND cycle_id=?',
                             (config_id, cycle_id)).fetchone()
        if cycle:
            limit = min(limit, float(cycle['budget']))
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='spot_budget_reservations'").fetchone()
        reservations = conn.execute('SELECT * FROM spot_budget_reservations WHERE config_id=?', (config_id,)).fetchall() if exists else []
        spent = _cycle_commitments(conn, config_id, cycle_id, reservations)
        committed, known = _known_commitments(conn, config_id)
        committed += sum(float(row['quote_cost']) for row in reservations
                         if row['status'] != 'released' and (row['symbol'], str(row['order_id'])) not in known)
    blocked = any(row['status'] in {'reserved', 'unknown'} for row in reservations)
    remaining = max(0, limit - spent)
    lifetime_remaining = max(0, lifetime_limit - committed) if lifetime_limit is not None else None
    return {'period_id': cycle_id, 'period_limit': limit, 'period_spent': spent,
            'period_remaining': remaining, 'lifetime_limit': lifetime_limit,
            'lifetime_committed': committed, 'lifetime_remaining': lifetime_remaining,
            'pending_unknown': blocked,
            'cycle_registered': bool(cycle),
            'available': 0 if blocked or not cycle else min(remaining, lifetime_remaining) if lifetime_remaining is not None else remaining}


def finish_spot_reservation(config_id: str, operation_id: str, status: str, order_id=None) -> None:
    with database.get_db_conn() as conn:
        _initialize(conn)
        conn.execute('UPDATE spot_budget_reservations SET status=?,order_id=COALESCE(?,order_id),updated_at=? '
                     'WHERE config_id=? AND operation_id=?',
                     (status, str(order_id) if order_id else None, time.time(), config_id, operation_id))
        conn.commit()


def reconcile_spot_reservations(config_id: str, config: dict, market_factory) -> int:
    """Recover ambiguous writes only from exact exchange order identity.

    Never release on an empty / failed search. An order can be absent from the
    exchange's recent history; that absence is not evidence of a failed write.
    """
    with database.get_db_conn() as conn:
        _initialize(conn)
        rows = [dict(row) for row in conn.execute('''SELECT * FROM spot_budget_reservations
            WHERE config_id=? AND (status='unknown' OR (status='reserved' AND updated_at<?))''',
            (config_id, time.time() - 60))]
        conn.commit()
    if not rows:
        return 0
    try:
        market = market_factory()
    except Exception:
        return 0
    recovered = 0
    for row in rows:
        try:
            candidates = []
            if row['order_id']:
                candidates = [market.exchange.fetch_order(row['order_id'], row['symbol'])]
            elif row['client_order_id']:
                for method in ('fetch_open_orders', 'fetch_closed_orders'):
                    try:
                        candidates.extend(getattr(market.exchange, method)(
                            row['symbol'], since=int(row['created_at'] * 1000) - 60000, limit=1000))
                    except Exception:
                        continue
                candidates = [order for order in candidates
                              if str(order.get('clientOrderId') or '') == row['client_order_id']]
            matches = {str(order.get('id')): order for order in candidates if order and order.get('id')}
            if len(matches) != 1:
                continue
            order_id, order = next(iter(matches.items()))
            if row['order_id'] and str(row['order_id']) != order_id:
                continue
            if (order.get('symbol') not in (None, row['symbol'])
                    or str(order.get('side') or 'buy').lower() != 'buy'):
                continue
            status = str(order.get('status') or '').lower()
            if status not in {'open', 'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'}:
                continue
            # CCXT unified filled is required even for zero-fill cancellations.
            if order.get('filled') is None:
                continue
            qty = float(order['filled'])
            price = float(order.get('average') or order.get('price') or row['entry_price'] or 0)
            cost = float(order.get('cost') or qty * price)
            if any(not math.isfinite(value) or value < 0 for value in (qty, price, cost)) or (qty > 0 and cost <= 0):
                continue
            local_status = ('FILLED' if status in {'closed', 'filled'} else
                            'CANCELLED' if status in {'canceled', 'cancelled', 'expired', 'rejected'} else
                            'PARTIAL' if qty > 0 else 'OPEN')
            with database.get_db_conn() as conn:
                exists = conn.execute('''SELECT 1 FROM orders WHERE config_id=? AND symbol=?
                    AND order_id=? AND trade_mode='SPOT_DCA'
                    AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''',
                    (config_id, row['symbol'], order_id)).fetchone()
            if not exists:
                database.save_order_log(order_id, row['symbol'], config.get('model', 'Unknown'), 'buy',
                                        row['entry_price'] or order.get('price') or price, 0, 0,
                                        '通过现货委托标识核验恢复', trade_mode='SPOT_DCA', config_id=config_id,
                                        amount=row['amount'] or order.get('amount') or qty, status=local_status)
                with database.get_db_conn() as conn:
                    created = datetime.fromtimestamp(row['created_at'], database.TZ_CN).strftime('%Y-%m-%d %H:%M:%S')
                    conn.execute('''UPDATE orders SET timestamp=? WHERE config_id=? AND symbol=?
                        AND order_id=? AND trade_mode='SPOT_DCA'
                        AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''',
                        (created, config_id, row['symbol'], order_id))
                    conn.commit()
            database.upsert_spot_order_fill(order_id, config_id, row['symbol'], local_status, qty, cost, price)
            finish_spot_reservation(config_id, row['operation_id'], 'submitted', order_id)
            recovered += 1
        except Exception:
            # Preserve the reservation when any part of reconciliation fails.
            continue
    if recovered:
        from backend.utils.trade_operations import reconcile_pending_trade_operations
        reconcile_pending_trade_operations(config_id)
    return recovered


def assert_owned_spot_order(market_tool, symbol: str, order_id: str, config_id: str | None = None) -> dict:
    """Require this task's spot creation evidence before any cancellation."""
    from backend.config import config as runtime_config

    config_id = str(config_id or getattr(market_tool, 'config_id', '') or '')
    if not config_id or not str(order_id).strip():
        raise ValueError('现货撤单必须提供任务和本任务托管订单 ID')
    config = runtime_config.get_config_by_id(config_id) or {}
    if config.get('mode', '').upper() != 'SPOT_DCA':
        raise ValueError('现货撤单仅允许现货任务')
    symbol = resolve_spot_symbol(config, symbol)
    with database.get_db_conn() as conn:
        rows = conn.execute("""SELECT config_id FROM orders WHERE order_id=? AND symbol=?
            AND trade_mode='SPOT_DCA' AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'
            AND UPPER(side) IN ('BUY','BUY_LIMIT')""", (str(order_id), symbol)).fetchall()
        if not any(row['config_id'] == config_id for row in rows):
            raise ValueError('该现货订单不属于当前任务及标的，禁止撤单')
    return {'source': 'spot_order_log', 'role': 'entry', 'trigger': False}
