"""Deterministic task-only history. Model and workbench share these definitions."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math

from backend import database
from backend.config import config
from backend.utils.spot_portfolio import get_config_symbols
from backend.utils.fill_classification import classify_fill


def number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def stamp_ms(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value*1000)
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return int((database.TZ_CN.localize(stamp) if stamp.tzinfo is None else stamp).timestamp()*1000)


def exit_order_outcomes(fills, *, since_ms, end_ms, currency):
    """One sample per executed exit order; an entry is not required for venue PnL."""
    orders = {}
    for fill in fills:
        if not fill.get('role') or fill['role'] == 'entry' or not (number(fill.get('amount')) or 0) > 0:
            continue
        when = fill.get('timestamp_ms')
        if when is not None and when >= end_ms:
            continue
        key = (fill.get('symbol'), fill['order_id'])
        row = orders.setdefault(key, dict(timestamp_ms=None, net=0.0, reason=None))
        if when is not None:
            row['timestamp_ms'] = max(row['timestamp_ms'] or 0, when)
        pnl = number(fill.get('realized_pnl'))
        parts = fill.get('fees') or []
        if pnl is None:
            row['reason'] = 'missing_pnl'
        elif not parts:
            row['reason'] = row['reason'] or 'incomplete_fees'
        else:
            row['net'] += pnl
            for fee in parts:
                cost = number(fee.get('cost'))
                if cost is None or not fee.get('currency'):
                    row['reason'] = row['reason'] or 'incomplete_fees'
                elif not currency or fee['currency'] != currency:
                    row['reason'] = row['reason'] or 'fee_currency_conversion_required'
                else:
                    row['net'] -= cost
    selected = [r for r in orders.values() if (since_ms == 0 if r['timestamp_ms'] is None
                                              else since_ms <= r['timestamp_ms'] < end_ms)]
    return selected


def summarize_history(fills: list[dict], cycles: list[dict], *, since_ms: int, end_ms: int,
                      currency: str | None, scan_complete=True, orders=None) -> dict:
    # Count first actual execution of a logical order, never each partial fill.
    entries = {}
    for fill in fills:
        if fill.get('role') == 'entry' and (fill.get('amount') or 0) > 0:
            key = (fill.get('symbol'), fill.get('logical_order_id') or fill['order_id'])
            previous = entries.get(key)
            if previous is None or (fill.get('timestamp_ms') or end_ms) < (previous.get('timestamp_ms') or end_ms):
                entries[key] = fill
    def in_period(row):
        when = row.get('timestamp_ms')
        return since_ms == 0 if when is None else since_ms <= when < end_ms
    opened = [r for r in entries.values() if in_period(r)]
    selected = [r for r in fills if in_period(r)]
    fees, gross, missing_fee, missing_pnl = {}, 0.0, 0, 0
    for fill in selected:
        pnl = number(fill.get('realized_pnl'))
        if pnl is None and fill.get('role') == 'entry':
            pnl = 0.0  # Opening alone does not realize a trading profit.
        if pnl is None:
            missing_pnl += 1
        else:
            gross += pnl
        parts = fill.get('fees') or []
        if not parts:
            missing_fee += 1
        for fee in parts:
            value = number(fee.get('cost'))
            if value is None or not fee.get('currency'):
                missing_fee += 1
            else:
                fees[fee['currency']] = fees.get(fee['currency'], 0.0) + value
    net_available = bool(selected) and currency is not None and not missing_pnl and not missing_fee and not (set(fees)-{currency})
    closed = [c for c in cycles if c.get('closed_at_ms') is not None and since_ms <= c['closed_at_ms'] < end_ms]
    nets, unknown = [], 0
    for cycle in closed:
        value, costs = number(cycle.get('realized_pnl_before_fees')), cycle.get('fees_by_currency') or {}
        if value is None or cycle.get('missing_pnl') or cycle.get('missing_fees') or not costs or not currency or set(costs)-{currency}:
            unknown += 1
        else:
            nets.append(value - costs.get(currency, 0.0))
    cycle_nets = nets
    outcomes = exit_order_outcomes(fills, since_ms=since_ms, end_ms=end_ms, currency=currency)
    nets = [r['net'] for r in outcomes if r['reason'] is None]
    unknown_orders = len(outcomes) - len(nets)
    wins, losses = [n for n in nets if n>1e-10], [n for n in nets if n < -1e-10]
    longs = sum(r.get('side') == 'LONG' for r in opened)
    shorts = sum(r.get('side') == 'SHORT' for r in opened)
    result = dict(open_count=len(opened), long_count=sum(r.get('side')=='LONG' for r in opened),
                short_count=sum(r.get('side')=='SHORT' for r in opened), buy_count=sum(r.get('side')=='BUY' for r in opened),
                long_pct=longs/(longs+shorts)*100 if longs+shorts else None,
                short_pct=shorts/(longs+shorts)*100 if longs+shorts else None,
                closed_cycles=len(closed), winning_cycles=sum(n>1e-10 for n in cycle_nets),
                losing_cycles=sum(n < -1e-10 for n in cycle_nets),
                breakeven_cycles=sum(abs(n)<=1e-10 for n in cycle_nets), unknown_cycles=unknown,
                closed_orders=len(outcomes), winning_orders=len(wins), losing_orders=len(losses),
                breakeven_orders=len(nets)-len(wins)-len(losses), unknown_orders=unknown_orders,
                win_rate=len(wins)/len(nets)*100 if nets else None,
                profit_loss_ratio=(sum(wins)/len(wins))/abs(sum(losses)/len(losses)) if wins and losses else None,
                ratio_status='ready' if wins and losses else 'no_exit_orders' if not outcomes else 'incomplete_fees' if unknown_orders else 'no_loss_samples' if wins else 'no_win_samples' if losses else 'breakeven_only',
                realized_pnl_before_fees=gross if selected and not missing_pnl and currency else None,
                realized_pnl_after_fees=gross-fees.get(currency,0) if net_available else None,
                fees_by_currency=fees, missing_fee_count=missing_fee, missing_pnl_count=missing_pnl,
                missing_time_count=sum(r.get('timestamp_ms') is None for r in fills),
                unknown_role_count=sum(not r.get('role') for r in selected),
                fill_count=len(selected), currency=currency, scan_complete=scan_complete)
    balances, missing_entry = {}, False
    for fill in sorted(fills, key=lambda r: r.get('timestamp_ms') or 0):
        key = (fill.get('symbol'), fill.get('side'))
        quantity = number(fill.get('amount')) or 0
        if fill.get('role') == 'entry':
            balances[key] = balances.get(key, 0) + quantity
        elif fill.get('role'):
            if quantity > balances.get(key, 0) + 1e-10 and in_period(fill):
                missing_entry = True
            balances[key] = max(0, balances.get(key, 0) - quantity)
    unknown_entries = result['unknown_role_count'] > 0 or missing_entry
    outcome_reason = (next((r['reason'] for r in outcomes if r['reason']), None) or
                      ('no_trades' if not selected else 'no_exit_orders' if not outcomes else 'history_incomplete'))
    profit_reason = ('no_trades' if not selected else 'missing_pnl' if missing_pnl else
                     'incomplete_fees' if missing_fee else 'fee_currency_conversion_required')
    def metric(value, reason, *, partial=False):
        return dict(value=value, status='partial' if value is not None and partial else 'ready' if value is not None else 'unavailable',
                    reason=reason if value is None or partial else None)
    result['metrics'] = {
        'open_count': metric(None if unknown_entries else len(opened), 'missing_entry_history' if missing_entry else 'unknown_order_role'),
        'realized_pnl_after_fees': metric(result['realized_pnl_after_fees'], profit_reason),
        'win_rate': metric(result['win_rate'], outcome_reason, partial=bool(unknown_orders or not scan_complete)),
        'profit_loss_ratio': metric(result['profit_loss_ratio'], outcome_reason if not nets else result['ratio_status'], partial=bool(unknown_orders or not scan_complete)),
        'long_short_ratio': metric(f'{longs}:{shorts}' if longs+shorts else None, 'no_direction_samples'),
    }
    order_rows = {}
    for order in orders or []:
        key = (order.get('symbol'), order['order_id'])
        if str(order.get('status', '')).lower() in {'rejected', 'failed', 'submitting', 'unknown'}:
            continue
        prior = order_rows.get(key)
        if prior is None or order.get('observed_at_ms', 0) >= prior.get('observed_at_ms', 0):
            order_rows[key] = order
    cohort = [r for r in order_rows.values() if in_period(r)]
    cancelled = sum(str(r.get('status', '')).lower() in {'canceled', 'cancelled'} for r in cohort)
    result.update(accepted_order_count=len(cohort), cancelled_order_count=cancelled,
                  cancel_rate=cancelled/len(cohort)*100 if cohort else None)
    result['metrics']['cancel_rate'] = metric(result['cancel_rate'], 'no_accepted_orders')
    result['diagnostics'] = dict(missing_entry_history=missing_entry, unknown_role_count=result['unknown_role_count'],
                               unknown_cycles=unknown, unknown_orders=unknown_orders,
                               missing_fee_count=missing_fee, missing_pnl_count=missing_pnl)
    return result


def _real_history(config_id, symbols, end_ms):
    from backend.utils.trade_evidence import configured_account_scope
    from backend.utils.execution_cycles import reconstruct_cycles
    scope = configured_account_scope(config_id)
    fills, cycles, scans = [], [], []
    with database.get_db_conn() as conn:
        rows = conn.execute('''SELECT f.*,l.role,l.payload AS metadata FROM execution_fills f
            LEFT JOIN execution_order_links l ON l.account_scope=f.account_scope AND l.symbol=f.symbol AND l.order_id=f.order_id
            WHERE f.config_id=%s AND f.account_scope=%s AND (f.symbol=ANY(%s) OR split_part(f.symbol,':',1)=ANY(%s)) AND f.timestamp_ms<%s
            ORDER BY f.timestamp_ms,f.trade_id''', (config_id, scope, symbols, [s for s in symbols if ':' not in s], end_ms)).fetchall()
        links = conn.execute('''SELECT symbol,order_id,payload FROM execution_order_links
            WHERE config_id=%s AND account_scope=%s
              AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))''',
            (config_id,scope,symbols,[s for s in symbols if ':' not in s]))
        parents = {(r['symbol'],r['order_id']): json.loads(r['payload'] or '{}').get('replaces_order_id') for r in links}
        for row in rows:
            payload, metadata = json.loads(row['payload']), json.loads(row['metadata'] or '{}')
            role, side = classify_fill(payload, metadata, row['role'])
            root, visited = row['order_id'], set()
            while parents.get((row['symbol'],root)) and root not in visited:
                visited.add(root)
                root = parents[(row['symbol'],root)]
            fills.append(dict(symbol=row['symbol'], order_id=row['order_id'], logical_order_id=root,
                timestamp_ms=row['timestamp_ms'], role=role, side=side,
                amount=payload.get('base_amount',payload.get('amount')), realized_pnl=payload.get('realized_pnl'),
                fees=payload.get('fees') or ([payload['fee']] if payload.get('fee') else [])))
        sync = [json.loads(r['payload']) for r in conn.execute('''SELECT payload FROM execution_sync
            WHERE account_scope=%s AND (symbol=ANY(%s) OR split_part(symbol,':',1)=ANY(%s))''',
            (scope,symbols,[s for s in symbols if ':' not in s]))]
    for symbol in sorted({r['symbol'] for r in rows}):
        rebuilt = reconstruct_cycles(config_id, 0, end_ms, scope, symbol)
        # A manual or unattributed exit is not a task trading outcome.
        cycles.extend(c for c in rebuilt['completed'] if all(e['owner_known'] for e in c['events']))
        scans.append(rebuilt['scan_complete'])
    return fills, cycles, all(scans), sync


def _mock_history(config_id, symbols):
    with database.get_db_conn() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM mock_orders WHERE config_id=%s AND symbol=ANY(%s)', (config_id,symbols))]
        events = [dict(r) for r in conn.execute("SELECT * FROM orders WHERE config_id=%s AND symbol=ANY(%s) AND trade_mode='STRATEGY' AND event_type IN ('ENTRY_FILLED','MANUAL_CLOSE','SL_HIT','TP_HIT') ORDER BY timestamp,id", (config_id,symbols))]
        history = [dict(r) for r in conn.execute("SELECT * FROM position_history WHERE config_id=%s AND symbol=ANY(%s) AND source IN ('mock_order','mock_aggregate')", (config_id,symbols))]
    fills, cycles = [], []
    entry_events = {r['parent_order_id']:r for r in reversed(events) if r['event_type']=='ENTRY_FILLED'}
    exits = [r for r in events if r['event_type']!='ENTRY_FILLED']
    history_by_id = {r['position_key']:r for r in history}
    def direction(side):
        return 'LONG' if 'BUY' in side or 'LONG' in side else 'SHORT'
    for row in rows:
        if row['is_filled'] or row['filled_amount'] > 0:
            event = entry_events.get(row['order_id'])
            prior = history_by_id.get(row['order_id'], {})
            fills.append(dict(symbol=row['symbol'], order_id=row['order_id'], role='entry', side=direction(row['side']),
                amount=row['filled_amount'] or prior.get('amount') or row['amount'],
                timestamp_ms=stamp_ms(event['timestamp'] if event else row['activated_at'] or row['timestamp']),
                realized_pnl=0, fees=[]))
        if row['status']=='CLOSED' and row.get('execution_mode')!='independent_exits' and not any(e['parent_order_id']==row['order_id'] for e in exits):
            fills.append(dict(symbol=row['symbol'],order_id=row['order_id'],role='exit',side=direction(row['side']),
                              amount=row['amount'],timestamp_ms=stamp_ms(row['close_time']),realized_pnl=row['realized_pnl'],fees=[]))
        if row['status']=='CLOSED' and row['is_filled'] and row['order_id'] not in history_by_id and row.get('execution_mode')!='independent_exits':
            cycles.append(dict(closed_at_ms=stamp_ms(row['close_time']),realized_pnl_before_fees=row['realized_pnl'],missing_fees=True))
    for event in exits:
        fills.append(dict(symbol=event['symbol'],order_id=event['parent_order_id'] or event['order_id'],role='exit',side=direction(event['side']),
                          amount=event['amount'],timestamp_ms=stamp_ms(event['timestamp']),realized_pnl=event['realized_pnl'],fees=[]))
    for row in history:
        raw=json.loads(row.get('raw_json') or '{}')
        if row['status']=='CLOSED' and not raw.get('expired'):
            cycles.append(dict(closed_at_ms=stamp_ms(row['closed_at']),realized_pnl_before_fees=row['realized_pnl'],missing_fees=True))
    return fills, cycles, True, []


def _spot_history(config_id, symbols):
    from backend.utils.spot_valuation import spot_orders, spot_fill_events
    fills, cycles = [], []
    for symbol in symbols:
        with database.get_db_conn() as conn:
            orders = spot_orders(conn,config_id,symbol)
            events = spot_fill_events(conn,config_id,symbol,orders)
        quantity=actual_quantity=cost=gross=0.0
        base = symbol.split('/')[0]
        fees, missing, contaminated = {}, False, False
        for event in events:
            amount, value, pnl = event['quantity'], event['cost'], 0.0
            fee, currency = event.get('fee'),event.get('fee_currency')
            base_fee = fee if currency == base and fee is not None else 0.0
            if event['side']=='buy':
                quantity+=amount
                actual_quantity+=amount-base_fee
                cost+=value
            elif quantity<=0 or amount+base_fee>actual_quantity+1e-10:
                pnl=None
                contaminated=True
            else:
                removed=cost*amount/quantity
                pnl=value-removed
                quantity=max(0,quantity-amount)
                actual_quantity=max(0,actual_quantity-amount-base_fee)
                cost-=removed
                gross+=pnl
            missing |= fee is None or not currency
            if fee is not None and currency:
                fees[currency]=fees.get(currency,0)+fee
            when=stamp_ms(event['timestamp'])
            fills.append(dict(symbol=symbol,order_id=event['order_id'],timestamp_ms=when,role='entry' if event['side']=='buy' else 'exit',
                              side='BUY',amount=amount,realized_pnl=pnl,
                              fees=[dict(cost=fee,currency=currency)] if currency else []))
            if actual_quantity<=1e-12 and event['side']=='sell':
                if not contaminated:
                    cycles.append(dict(closed_at_ms=when,realized_pnl_before_fees=gross,missing_fees=missing,fees_by_currency=fees))
                quantity=actual_quantity=cost=gross=0.0
                fees,missing,contaminated={},False,False
    return fills,cycles,True,[]


def task_performance(config_id: str, *, now=None, cfg=None) -> dict:
    cfg=cfg or config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError('Config not found')
    now=now or datetime.now(database.TZ_CN)
    end_ms=int(now.timestamp()*1000)
    symbols=get_config_symbols(cfg)
    quotes={s.split(':')[-1] if ':' in s else s.split('/')[-1] for s in symbols}
    currency=next(iter(quotes)) if len(quotes)==1 else None
    mode=cfg.get('mode','STRATEGY')
    fills,cycles,complete,sync=(_real_history(config_id,symbols,end_ms) if mode=='REAL' else
                               _spot_history(config_id,symbols) if mode=='SPOT_DCA' else _mock_history(config_id,symbols))
    from backend.utils.task_order_history import task_orders
    orders = task_orders(config_id, symbols, mode)
    beginning=end_ms-7*24*3600*1000
    result=dict(config_id=config_id,scope='task',symbols=symbols,mode=mode,as_of=now.isoformat(),
                coverage_start_ms=min((r['timestamp_ms'] for r in fills if r.get('timestamp_ms') is not None),default=None),
                history_scope='all_recorded', window_complete=False,
                note='仅当前任务已归属记录；全历史指本地可查询历史。胜率按平仓成交订单的已实现盈亏扣该单手续费计算，保本计入分母；同单分批成交合并，按最后成交时间归期，不分摊开仓费。取消率为该期间受理订单中已成功撤销的比例，失败调用不计数。')
    result['seven_days']=summarize_history(fills,cycles,since_ms=beginning,end_ms=end_ms,currency=currency,scan_complete=complete,orders=orders)
    result['all_history']=summarize_history(fills,cycles,since_ms=0,end_ms=end_ms,currency=currency,scan_complete=complete,orders=orders)
    result['window_complete']=bool(sync) and all(any(left<=beginning and right>=end_ms-1 for left,right in s.get('covered_intervals',[])) for s in sync)
    if mode == 'REAL' and not result['window_complete']:
        for period in ('seven_days', 'all_history'):
            for metric in result[period]['metrics'].values():
                if metric['status'] == 'ready':
                    metric.update(status='partial', reason='history_incomplete')
    return result


def task_history_context(config_id: str, cfg=None) -> str:
    try:
        data=task_performance(config_id,cfg=cfg)
    except Exception:
        return '当前任务历史统计读取失败；不能据此断言没有交易或收益为零。'
    lines=['当前任务统计（非实时持仓；只统计已记录、已归属成交）：']
    for key,label in [('seven_days','最近7天'),('all_history','全部历史')]:
        row=data[key]
        def fmt(value):
            return f'{value:.2f}' if value is not None else '未知/无样本'
        fees=', '.join(f'{v:.6g} {k}' for k,v in row['fees_by_currency'].items()) or '无已知费用'
        lines.append(f"{label}：开仓/加仓{fmt(row['metrics']['open_count']['value'])}单（多{row['long_count']}/空{row['short_count']}/现货买入{row['buy_count']}）；"
                     f"平仓成交{row['closed_orders']}单，扣平仓费胜率{fmt(row['win_rate'])}%，盈亏比{fmt(row['profit_loss_ratio'])}；"
                     f"多空比{row['metrics']['long_short_ratio']['value'] or '无方向样本'}，取消率{fmt(row['cancel_rate'])}%（撤单{row['cancelled_order_count']}/受理{row['accepted_order_count']}）；"
                     f"已实现费前{fmt(row['realized_pnl_before_fees'])}/扣费后{fmt(row['realized_pnl_after_fees'])} {row['currency'] or '币种不统一'}；"
                     f"费用{fees}（缺失{row['missing_fee_count']}）；保本平仓单{row['breakeven_orders']}，未知平仓单{row['unknown_orders']}。")
    lines.append(f"记录起点：{datetime.fromtimestamp(data['coverage_start_ms']/1000,database.TZ_CN).isoformat() if data['coverage_start_ms'] is not None else '无已知时间'}；截至{data['as_of']}；7天覆盖完整：{data['window_complete']}；周期扫描完整：{data['all_history']['scan_complete']}。{data['note']}")
    return '\n'.join(lines)
