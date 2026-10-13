"""Render independent exits from supplied snapshots without another exchange request."""
from __future__ import annotations

import math
from backend.utils.exit_orders import canonical_order_type, exit_group, order_reference, observed_order_status


def independent_exit_chart_lines(snapshot: dict) -> list[dict]:
    """Keep trigger and execution prices distinct, including legacy ordinary TP limits."""
    lines = []
    for order in snapshot.get('exits', []):
        kind = canonical_order_type(order)
        group = exit_group(order)
        label = {'stop_loss': 'SL', 'take_profit': 'TP', 'limit': 'LIMIT'}.get(group, kind)
        for field, suffix in (('trigger_price', ' TRIGGER'), ('price', ' LIMIT' if kind != 'limit' else '')):
            if not order.get(field):
                continue
            lines.append({'price': order[field], 'type': group, 'label': label + suffix,
                          'amount': None if order.get('quantity_mode') == 'all' else order.get('remaining'),
                          'quantity_mode': order.get('quantity_mode', 'fixed'), 'order_type': kind,
                          'side': order.get('pos_side'), 'order_id': order.get('order_id'),
                          'order_status': observed_order_status(order)})
    return lines


def independent_exit_context(account: dict, *, plans: list[dict] | None = None,
                             contract_size: float | None = None, symbol: str = '') -> str:
    snapshot = account.get('independent_exits')
    local_real = snapshot is None
    units = '标的币数量'
    cycle_notes = []
    if local_real:
        snapshot = {'exits': [], 'unresolved_orders': [], 'uncovered': {'LONG': 0.0, 'SHORT': 0.0}, 'pending': False}
        quantities = {side: sum(float(p.get('amount') or 0) for p in account.get('real_positions', [])
                               if str(p.get('side')).upper() == side) for side in ('LONG', 'SHORT')}
        size_known = isinstance(contract_size, (int, float)) and math.isfinite(contract_size) and contract_size > 0
        size = contract_size if size_known else 1
        exposure = dict(quantities)
        fixed_groups = {}
        coverage_unknown = not size_known or bool(account.get('error')) or plans is None
        if not size_known:
            units = '合约张数（合约面值不可用，不换算币数量）'
        visible_sides = {side for side, quantity in quantities.items() if quantity > 0}
        managed_sides = set()
        cycles_readable = plans is not None and not account.get('error')
        canonical = lambda value: str(value or '').upper() if ':' in str(value or '') else str(value or '').upper() + ':' + str(value or '').split('/')[-1].upper()
        for plan in plans or []:
            if symbol and canonical(plan.get('symbol')) != canonical(symbol):
                continue
            if plan.get('read_error'):
                snapshot['pending'] = True
                coverage_unknown = True
                cycles_readable = False
                continue
            if plan.get('execution_mode') != 'independent_exits' or plan.get('state') == 'DONE':
                continue
            managed_sides.add(plan.get('side'))
            from backend.utils.trade_receipts import unresolved_orders
            snapshot['unresolved_orders'].extend(unresolved_orders(plan))
            plan_pending = bool(plan.get('position_error') or plan.get('ownership_error') or plan.get('state') == 'EXITING'
                                or plan.get('unobserved_entry_fills') or plan.get('cleanup_fill_unseen')
                                or any(r.get('fill_pending') or r.get('replacement')
                                       for r in plan.get('entries', []) + plan.get('exits', [])))
            cycle_notes.append(f"{plan.get('side')} 本任务独立退出周期：{plan.get('state')}；待核验={plan_pending}")
            if plan.get('adoption'):
                adoption = plan['adoption']
                cycle_notes.append(f"{plan.get('side')} 来源为用户委托接管的手动仓；接管基线={adoption.get('amount')} 标的币，时间={adoption.get('adopted_at')}；不是本任务开仓成交。")
            snapshot['pending'] |= plan_pending
            for record in plan.get('exits', []):
                snapshot['pending'] |= bool(record.get('replacement') or record.get('fill_pending'))
                status = str(record.get('status') or 'unknown').lower()
                if status in {'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'expired'}:
                    continue
                all_quantity = record.get('quantity_mode') == 'all'
                remaining = None if all_quantity else max(float(record.get('amount') or 0) - float(record.get('filled') or 0), 0) * size
                active = status in {'open', 'new', 'waiting_trigger', 'partially_filled', 'triggered'}
                pending = plan_pending or not record.get('id') or not active
                side = plan.get('side')
                group = exit_group(record)
                snapshot['exits'].append({**record, 'order_id': order_reference(record),
                                         'order_type': canonical_order_type(record), 'pos_side': side,
                                         'order_status': observed_order_status(record),
                                         'remaining': remaining, 'pending': pending})
                snapshot['pending'] |= pending
                if not all_quantity:
                    fixed_groups[(side, group)] = fixed_groups.get((side, group), 0) + remaining
                if size_known and group == 'stop_loss' and not pending:
                    if side in quantities:
                        quantities[side] = max(0, quantities[side] - (exposure[side] if all_quantity else remaining))
        snapshot['quantity_conflict'] = [
            {'side': side, 'group': group, 'fixed_remaining': quantity, 'position_amount': exposure.get(side, 0)}
            for (side, group), quantity in fixed_groups.items()
            if size_known and quantity > exposure.get(side, 0) + 1e-12
        ]
        snapshot['uncovered'] = quantities if not coverage_unknown else {'LONG': None, 'SHORT': None}
        if not cycles_readable:
            snapshot['pending'] = True
            cycle_notes.append('持仓或本任务周期记录读取不完整，归属未知。')
        else:
            for side in sorted(visible_sides - managed_sides):
                snapshot['pending'] = True
                cycle_notes.append(f'{side} 可见交易所持仓，但本任务没有活跃独立退出周期。')
    lines = [f'独立退出单（{units}；' + ('本地核验记录非实时成交证明' if local_real else '模拟账户快照') + '）。']
    lines.extend(cycle_notes)
    if snapshot.get('unresolved_orders'):
        import json
        lines.append('具体待核验订单（不计入有效保护）：' + json.dumps(snapshot['unresolved_orders'], ensure_ascii=False))
    for order in snapshot.get('exits', []):
        remaining_text = order.get('remaining')
        if order.get('quantity_mode') == 'all':
            remaining_text = (f"全部（提交前快照；已解析={order.get('resolved_amount')}）"
                              if canonical_order_type(order) == 'market' else '全部（触发时该方向仓位）')
        lines.append(
            f"ID={order.get('order_id') or order.get('id') or order.get('client_id') or '未确认'} | "
            f"{order.get('pos_side')} | {canonical_order_type(order)} | price={order.get('price')} | "
            f"trigger={order.get('trigger_price')} | remaining={remaining_text} | "
            f"status={observed_order_status(order)} | pending={bool(order.get('pending'))}"
        )
    if not snapshot.get('exits'):
        lines.append('本任务记录活跃独立退出单：0。')
    coverage = snapshot.get('uncovered') or {}
    lines.append('本系统独立 SL 未覆盖数量：' + '；'.join(f"{side}={coverage.get(side) if coverage.get(side) is not None else '未知'}" for side in ('LONG', 'SHORT')))
    lines.append('覆盖为当前挂单数量估计；条件单等待触发，SL LIMIT 触发后仍需限价成交，不保证退出。')
    if snapshot.get('quantity_conflict'):
        lines.append(f"quantity_conflict={snapshot.get('quantity_conflicts') or snapshot['quantity_conflict']}；固定档保持原数量，由 Agent 决定 edit/cancel。")
    lines.append(f"待核验={bool(snapshot.get('pending'))}")
    return '\n'.join(lines)
