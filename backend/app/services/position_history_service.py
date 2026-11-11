"""Local evidence adapters for the position history and spot statistics APIs."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from backend import database
from backend.config import config
from backend.utils.position_history import build_positions, numeric, timestamp, iso_time
from backend.utils.spot_portfolio import get_config_symbols
from backend.utils.spot_valuation import spot_orders, spot_fill_events


def _real_events(conn, config_id, symbols, scope, end_ms):
    from backend.utils.execution_cycles import reconstruct_cycles
    from backend.utils.fill_classification import classify_fill
    events = []
    rows = conn.execute('''SELECT f.*, l.role, l.payload AS metadata FROM execution_fills f
        LEFT JOIN execution_order_links l ON l.account_scope=f.account_scope
          AND l.symbol=f.symbol AND l.order_id=f.order_id
        WHERE f.config_id=%s AND f.account_scope=%s AND f.timestamp_ms<=%s
          AND (f.symbol=ANY(%s) OR split_part(f.symbol,':',1)=ANY(%s))
        ORDER BY f.timestamp_ms,LENGTH(f.trade_id),f.trade_id''',
        (config_id, scope, end_ms, symbols, [s for s in symbols if ':' not in s])).fetchall()
    # Reuse the existing account-aware cycle audit to detect mixed ownership.
    foreign, incomplete, incomplete_trades = set(), set(), set()
    for symbol in {r['symbol'] for r in rows}:
        cycles = reconstruct_cycles(config_id, 0, end_ms + 1, scope, symbol, read_conn=conn)
        for cycle in cycles['excluded_cycles'] + cycles['completed'] + cycles['open_cycles']:
            if cycle.get('incomplete'):
                incomplete_trades.update((symbol, str(e['trade_id'])) for e in cycle['events'])
            if cycle.get('foreign_entry') or any(not e['owner_known'] for e in cycle['events']):
                foreign.update((symbol, str(e['trade_id'])) for e in cycle['events'])
        if not cycles['scan_complete']:
            incomplete.add(symbol)
    for index, row in enumerate(rows):
        payload, metadata = json.loads(row['payload']), json.loads(row['metadata'] or '{}')
        role, side = classify_fill(payload, metadata, row['role'])
        issues = []
        if (row['symbol'], str(row['trade_id'])) in foreign:
            issues.append('foreign_activity')
        if row['symbol'] in incomplete or (row['symbol'], str(row['trade_id'])) in incomplete_trades:
            issues.append('history_incomplete')
        if not role or side not in {'LONG', 'SHORT'}:
            issues.append('unknown_execution')
        events.append(dict(event_id=f"trade:{row['trade_id']}", trade_id=str(row['trade_id']),
            order_id=row['order_id'], symbol=row['symbol'], side=side or 'UNKNOWN',
            kind='entry' if role == 'entry' else 'exit' if role else 'unknown',
            timestamp_ms=row['timestamp_ms'], sequence=index,
            quantity=numeric(payload.get('base_amount', payload.get('amount'))), price=numeric(payload.get('price')),
            pnl=numeric(payload.get('realized_pnl')) if role != 'entry' else 0.0,
            fees=payload.get('fees') or ([payload['fee']] if payload.get('fee') else []),
            source='execution_fills', reason=metadata.get('reason'), issues=issues, is_fill=True))
    return events


def _mock_events(conn, config_id, symbols):
    rows = [dict(r) for r in conn.execute('''SELECT * FROM orders WHERE config_id=%s
        AND symbol=ANY(%s) AND trade_mode='STRATEGY'
        AND event_type IN ('ENTRY_FILLED','MANUAL_CLOSE','SL_HIT','TP_HIT') ORDER BY timestamp,id''',
        (config_id, symbols))]
    events, entries, exits = [], set(), set()
    for row in rows:
        entry = row['event_type'] == 'ENTRY_FILLED'
        oid = row['parent_order_id'] or row['order_id']
        (entries if entry else exits).add(oid)
        side = 'LONG' if 'BUY' in row['side'] or 'LONG' in row['side'] else 'SHORT'
        events.append(dict(event_id=f"event:{row['id']}", trade_id=row['order_id'], order_id=oid,
            symbol=row['symbol'], side=side, kind='entry' if entry else 'exit',
            timestamp_ms=timestamp(row['timestamp']), sequence=row['id'], quantity=numeric(row['amount']),
            price=numeric(row['entry_price']), pnl=0.0 if entry else numeric(row['realized_pnl']),
            fees=[], source='mock_event', reason=row['reason'], is_fill=True))
    histories = {r['position_key']: dict(r) for r in conn.execute('''SELECT * FROM position_history
        WHERE config_id=%s AND symbol=ANY(%s) AND source='mock_order' ''', (config_id, symbols))}
    for row in conn.execute('SELECT * FROM mock_orders WHERE config_id=%s AND symbol=ANY(%s)', (config_id, symbols)):
        if not row['is_filled'] and not (row['filled_amount'] or 0) > 0:
            continue
        side = 'LONG' if 'BUY' in row['side'] else 'SHORT'
        prior = histories.get(row['order_id'], {})
        if row['order_id'] not in entries:
            # Order timestamp / activation is not evidence of actual execution.
            events.append(dict(event_id=f"legacy-entry:{row['order_id']}", trade_id=None,
                order_id=row['order_id'], symbol=row['symbol'], side=side, kind='entry', timestamp_ms=None,
                quantity=numeric(row['filled_amount'] or prior.get('amount') or row['amount']),
                price=numeric(row['price']), pnl=0.0, fees=[], source='mock_order', is_fill=False,
                reason=row['reason'], issues=['incomplete_fill_details']))
        if row['status'] == 'CLOSED' and row.get('execution_mode') != 'independent_exits' and row['order_id'] not in exits:
            events.append(dict(event_id=f"legacy-exit:{row['order_id']}", trade_id=None,
                order_id=row['order_id'], symbol=row['symbol'], side=side, kind='exit',
                timestamp_ms=timestamp(row['close_time']), quantity=numeric(row['amount']),
                price=numeric(row['close_price']), pnl=numeric(row['realized_pnl']), fees=[],
                source='mock_order', is_fill=False, issues=['incomplete_fill_details']))
    return events


def _spot_events(conn, config_id, symbols, cfg):
    events, invested, errors = [], {}, {}
    for symbol in symbols:
        try:
            orders = spot_orders(conn, config_id, symbol, evidence_only=True)
            buys = [order['cost'] for order in orders.values() if order['side'] == 'buy']
            invested[symbol] = sum(buys) if all(value is not None for value in buys) else None
            fills = spot_fill_events(conn, config_id, symbol, orders, strict=False)
        except ValueError as exc:
            invested[symbol] = None
            errors[symbol] = str(exc)
            # Raw known executions are still evidence when cumulative totals
            # conflict. No invented residual execution is added in this case.
            fills = [dict(order_id=r['order_id'], trade_id=r['trade_id'], evidence_id=f"trade:{r['trade_id']}",
                side=r['side'], quantity=r['amount'], cost=r['cost'], timestamp=r['timestamp'],
                fee=r['fee'], fee_currency=r['fee_currency'], coarse=False) for r in conn.execute('''
                SELECT t.* FROM trade_history t WHERE t.symbol=%s AND t.config_id=%s
                AND EXISTS (SELECT 1 FROM orders o WHERE o.order_id=t.order_id AND o.symbol=t.symbol
                    AND o.config_id=%s AND o.trade_mode='SPOT_DCA') ORDER BY t.timestamp,t.storage_id''',
                (symbol, config_id, config_id))]
        for index, fill in enumerate(fills):
            amount, value = numeric(fill['quantity']), numeric(fill['cost'])
            issues = ['incomplete_fill_details'] if fill.get('coarse') else []
            if symbol in errors:
                issues.append('conflicting_evidence')
            events.append(dict(event_id=fill['evidence_id'], trade_id=fill.get('trade_id'),
                order_id=fill['order_id'], symbol=symbol, side='SPOT',
                kind='entry' if fill['side'] == 'buy' else 'exit', timestamp_ms=timestamp(fill['timestamp']),
                sequence=index, quantity=amount, cost=value, price=value / amount if amount and value is not None else None,
                fees=[dict(cost=fill['fee'], currency=fill['fee_currency'])] if fill.get('fee_currency') else [],
                source='trade_history' if not fill.get('coarse') else 'order_total', issues=issues,
                is_fill=not fill.get('coarse'), ambiguous_time=bool(fill.get('coarse') and fill.get('first_time') != fill.get('last_time'))))
        revisions = [dict(r) for r in conn.execute('''SELECT * FROM spot_cost_basis_revisions
            WHERE config_id=%s AND symbol=%s ORDER BY revision''', (config_id, symbol))]
        current = conn.execute('SELECT * FROM spot_cost_basis WHERE config_id=%s AND symbol=%s', (config_id, symbol)).fetchone()
        if current and not any(r['revision'] == current['revision'] and r['updated_at'] == current['updated_at'] for r in revisions):
            revisions.append(dict(current))
        if symbol == cfg.get('symbol') and numeric(cfg.get('initial_qty')):
            quantity = numeric(cfg['initial_qty'])
            cost = quantity * numeric(cfg['manual_avg_cost']) if numeric(cfg.get('manual_avg_cost')) else numeric(cfg.get('initial_cost'))
            # A legacy baseline has no start time; it is not a historical buy.
            events.append(dict(event_id='legacy-basis', symbol=symbol, side='SPOT', kind='calibration',
                timestamp_ms=None, quantity=quantity, cost=cost, fees=[], is_fill=False, source='legacy_basis', initial=True))
        for revision in revisions:
            events.append(dict(event_id=f"basis:{revision['revision']}:{revision['updated_at']}",
                symbol=symbol, side='SPOT', kind='calibration', timestamp_ms=timestamp(revision['updated_at']),
                sequence=10**12, quantity=revision['quantity'], cost=revision['quantity'] * revision['avg_cost'],
                price=revision['avg_cost'], fees=[], is_fill=False, source='cost_basis'))
    # Attach stored reasons without inferring a reason from a later outcome.
    reasons = {}
    for row in conn.execute('''SELECT symbol,order_id,reason FROM orders WHERE config_id=%s
        AND trade_mode='SPOT_DCA' AND symbol=ANY(%s) AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'
        ORDER BY id''', (config_id, symbols)):
        reasons.setdefault((row['symbol'], row['order_id']), row['reason'])
    for event in events:
        event['reason'] = reasons.get((event['symbol'], event.get('order_id')))
    return events, invested, errors


def read_position_history(config_id: str, *, cfg=None, now=None) -> dict:
    cfg = cfg or config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError('Config not found')
    mode, symbols = cfg.get('mode', 'STRATEGY'), get_config_symbols(cfg)
    now = now or datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    scope = f'{mode}:{config_id}'
    if mode == 'REAL':
        from backend.utils.trade_evidence import configured_account_scope
        scope = configured_account_scope(config_id)
    # All source reads share a consistent, read-only database snapshot.
    with database.get_db_conn() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        invested, errors = {}, {}
        if mode == 'REAL':
            events = _real_events(conn, config_id, symbols, scope, end_ms)
        elif mode == 'SPOT_DCA':
            events, invested, errors = _spot_events(conn, config_id, symbols, cfg)
        else:
            events = _mock_events(conn, config_id, symbols)
    positions = build_positions(config_id, mode, scope, events, now_ms=end_ms)
    return dict(config_id=config_id, mode=mode, symbols=symbols, as_of=iso_time(end_ms),
                history_scope='all_recorded_task', positions=positions, invested=invested, errors=errors,
                complete=not errors and all(p['complete'] for p in positions))


def position_page(config_id: str, *, symbol=None, status=None, page=1, per_page=20) -> dict:
    data = read_position_history(config_id)
    rows = [p for p in data['positions'] if (symbol is None or p['symbol'] == symbol)
            and (status is None or p['status'] == status)]
    items = [{k: v for k, v in row.items() if k != 'events'} for row in rows[(page-1)*per_page:page*per_page]]
    return dict(success=True, items=items, total=len(rows), page=page, per_page=per_page,
                symbols=data['symbols'], as_of=data['as_of'], complete=data['complete'], errors=data['errors'])


def position_detail(config_id: str, position_id: str) -> dict:
    data = read_position_history(config_id)
    row = next((p for p in data['positions'] if p['position_id'] == position_id), None)
    if row is None:
        raise FileNotFoundError('Position not found in this task')
    return dict(success=True, position=row, as_of=data['as_of'])


def spot_statistics(config_id: str, *, cfg=None, now=None) -> dict:
    data = read_position_history(config_id, cfg=cfg, now=now)
    end = timestamp(data['as_of'])
    events = [event for p in data['positions'] for event in p['events'] if event['kind'] != 'calibration']
    total = sum(data['invested'].values()) if all(v is not None for v in data['invested'].values()) else None
    def summarize(symbol, since):
        all_events = [e for e in events if symbol is None or e['symbol'] == symbol]
        selected = [e for e in all_events if (since == 0 if e.get('timestamp_ms') is None else since <= e['timestamp_ms'] <= end)]
        actual = [e for e in selected if e['is_fill']]
        exits = [e for e in selected if e['kind'] in {'exit', 'reduce', 'close'}]
        untimed = any(e.get('timestamp_ms') is None or e.get('ambiguous_time') for e in all_events)
        incomplete = any(not e['is_fill'] for e in selected) or bool(since and untimed)
        known = sum(e['realized_net_pnl'] for e in exits if e['realized_net_pnl'] is not None)
        net_known = (not any(e['realized_net_pnl'] is None for e in exits) and not (since and untimed)
                     and not (data['errors'] if symbol is None else symbol in data['errors']))
        fees = {}
        for event in selected:
            for fee in event['fees']:
                if numeric(fee.get('cost')) is not None and fee.get('currency'):
                    fees[fee['currency']] = fees.get(fee['currency'], 0.0) + fee['cost']
        return dict(buy_fill_count=sum(e['kind'] in {'entry', 'add'} for e in actual),
                    sell_fill_count=sum(e['kind'] in {'exit', 'reduce', 'close'} for e in actual),
                    fill_count=len(actual), counts_complete=not incomplete,
                    realized_net_pnl=known if net_known else None, known_realized_net_pnl=known,
                    fees_by_currency=fees, complete=not incomplete and net_known,
                    issues=sorted({issue for e in selected for issue in e['issues']} | ({'missing_execution_time'} if since and untimed else set())))
    return dict(as_of=data['as_of'], cumulative_buy_amount=total,
        currency=data['symbols'][0].split('/')[-1],
        by_symbol=[dict(symbol=s, cumulative_buy_amount=data['invested'].get(s),
            investment_share_pct=data['invested'][s] / total * 100 if total and data['invested'].get(s) is not None else None,
            seven_days=summarize(s, end-7*86400000), all_history=summarize(s, 0)) for s in data['symbols']],
        seven_days=summarize(None, end-7*86400000), all_history=summarize(None, 0), errors=data['errors'])
