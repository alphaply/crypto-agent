"""Compact historical performance and explicit on-demand accounting evidence."""
from backend.storage import record as storage_record

import math

import backend.database as database


def equity_metrics(rows) -> dict:
    """Use the first valid positive snapshot as baseline; retain subsequent losses."""
    first = latest = None
    peak = max_drawdown = 0.0
    count = 0
    for row in rows:
        try:
            value = float(row['total_equity'])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or (first is None and value <= 0):
            continue
        point = {'timestamp': row['timestamp'], 'equity': value}
        if first is None:
            first = point
        latest = point
        count += 1
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    if first is None:
        return {'available': False, 'samples': 0}
    return {
        'available': count >= 2, 'samples': count,
        'start': first, 'latest': latest,
        'return_pct': (latest['equity'] / first['equity'] - 1) * 100,
        'drawdown_pct': (peak - latest['equity']) / peak * 100,
        'max_drawdown_pct': max_drawdown,
    }


def profit_metric(ledger: dict, currency: str = 'USDT') -> dict:
    """Realized fills, including partial exits; cycle completion is irrelevant."""
    account_wide = ledger.get('statistics_scope') == 'account_symbol'
    result = dict(available=False, amount=None, currency=currency,
                  basis='realized_after_quote_fees', scope='account_symbol' if account_wide else 'agent_attributed_fills',
                  reason='incomplete_evidence')
    complete = ((ledger.get('fill_count', 0) > 0 if account_wide else
                 ledger.get('window_complete') is True and ledger.get('unknown_owner_fill_count', 0) == 0)
                and ledger.get('missing_pnl_count', 0) == 0
                and ledger.get('missing_fee_count', 0) == 0)
    fees = ledger.get('fees_by_currency') or {}
    gross = ledger.get('known_realized_pnl_before_fees')
    if complete and isinstance(gross, (int, float)) and math.isfinite(gross):
        if set(fees) - {currency}:
            result['reason'] = 'fee_currency_conversion_required'
        elif all(isinstance(v, (int, float)) and math.isfinite(v) for v in fees.values()):
            result.update(available=True, amount=gross - fees.get(currency, 0), reason=None)
    return result


def performance_data(config_id: str, symbol: str, mode: str, *, hours: int = 168, now=None,
                     include_details: bool = False) -> dict:
    from datetime import datetime, timedelta
    from backend.database import TZ_CN
    mode = str(mode).upper()
    if mode not in {'REAL', 'STRATEGY'}:
        raise ValueError('Performance supports REAL and STRATEGY')
    if not 1 <= hours <= 24 * 366:
        raise ValueError('hours must be between 1 and 8784')
    now = now or datetime.now(TZ_CN)
    if now.tzinfo is None:
        now = TZ_CN.localize(now)
    beginning = now - timedelta(hours=hours)
    start, end = beginning.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')
    table = 'balance_history' if mode == 'REAL' else 'mock_balance_history'
    currency = symbol.split(':')[-1] if ':' in symbol else symbol.split('/')[-1]
    metric = {'available': False, 'samples': 0}
    errors = []
    ledger, closed = {}, []
    try:
        with database.get_db_conn() as conn:
            rows = conn.execute(f'SELECT timestamp,total_equity FROM {table} '
                                'WHERE config_id=%s AND symbol=%s AND timestamp>=%s AND timestamp<=%s ORDER BY timestamp,id',
                                (config_id, symbol, start, end)).fetchall()
        metric = equity_metrics(rows)
    except Exception:
        errors.append('equity_unavailable')
    try:
        if mode == 'REAL':
            from backend.utils.execution_ledger import recent_activity_data
            ledger = recent_activity_data(config_id, symbol, now, hours=hours, include_cycles=include_details)
            profit = profit_metric(ledger, currency)
        else:
            # Mock_orders are the canonical local close/partial-exit records.
            # Read nullable PnL directly: legacy helper coerces NULL to zero.
            with database.get_db_conn() as conn:
                closed = [storage_record(row) for row in conn.execute(
                    "SELECT * FROM mock_orders WHERE config_id=%s AND (symbol=%s OR symbol=%s) "
                    "AND status='CLOSED' AND close_time>=%s AND close_time<=%s ORDER BY close_time,order_id",
                    (config_id, symbol, symbol.split(':')[0], start, end))]
            values = [row.get('realized_pnl') for row in closed]
            profit = dict(available=all(isinstance(v, (int, float)) and math.isfinite(v) for v in values),
                          amount=sum(values) if all(isinstance(v, (int, float)) and math.isfinite(v) for v in values) else None,
                          currency=currency, basis='mock_realized_pnl', scope='local_strategy_orders', reason=None)
            if not profit['available']:
                profit['reason'] = 'missing_realized_pnl'
    except Exception:
        errors.append('profit_unavailable')
        profit = dict(available=False, amount=None, currency=currency,
                      basis='realized_after_quote_fees' if mode == 'REAL' else 'mock_realized_pnl',
                      scope='account_symbol' if mode == 'REAL' else 'local_strategy_orders', reason='read_failed')
    equity = dict(available=metric['available'], start=metric.get('start'), latest=metric.get('latest'),
                  change=(metric['latest']['equity'] - metric['start']['equity']) if metric['available'] else None,
                  return_pct=metric.get('return_pct') if metric['available'] else None, scope='account_snapshots', currency=currency)
    drawdown = dict(available=metric['available'], current_pct=metric.get('drawdown_pct') if metric['available'] else None,
                    max_pct=metric.get('max_drawdown_pct') if metric['available'] else None, scope='snapshot')
    result = dict(config_id=config_id, symbol=symbol, mode=mode, window_hours=hours,
                  from_time=start, as_of=end, equity=equity, profit=profit, drawdown=drawdown,
                  note='权益与回撤为本地快照，未剔除划转或共享账户影响；盈亏按归属成交，三者不可相加。')
    if mode == 'REAL':
        result['note'] = '实盘盈亏按当前交易所账户下该标的全部已记录成交计算，包含其他任务和手动交易；权益、回撤与成交盈亏不可相加。'
        result['window_complete'] = ledger.get('window_complete', False)
        if not result['window_complete']:
            result['note'] += '同步尚未完整覆盖所选时段，金额仅代表本地已记录成交。'
    if include_details:
        if mode == 'REAL':
            try:
                from backend.utils.execution_ledger import closed_positions_from_activity
                closed = closed_positions_from_activity(ledger)
            except Exception:
                errors.append('closed_position_details_unavailable')
        result['details'] = dict(ledger=ledger, closed_positions=closed, equity=metric,
                                 as_of=end, from_time=start, errors=errors)
    return result


def performance_context(config_id: str, symbol: str, mode: str) -> str:
    """Compact deterministic metrics. Full evidence is available only on demand."""
    if str(mode).upper() not in {'REAL', 'STRATEGY'}:
        return ''
    data = performance_data(config_id, symbol, mode)
    e, p, d = data['equity'], data['profit'], data['drawdown']
    change = f"{e['change']:+.2f} {e['currency']} ({e['return_pct']:+.2f}%)" if e['available'] else '暂不可统计'
    profit = f"{p['amount']:+.2f} {p['currency']}" if p['available'] else '暂不可完整统计'
    drawdown = f"当前 {d['current_pct']:.2f}% / 最大 {d['max_pct']:.2f}%" if d['available'] else '暂不可统计'
    return (f"权益变化：{change}；盈亏：{profit}；快照回撤：{drawdown}。\n"
            f"区间 {data['from_time']} → {data['as_of']}。{data['note']}")
