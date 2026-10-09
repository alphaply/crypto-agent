"""Render independent exits from supplied snapshots without another exchange request."""
from __future__ import annotations

import math


def independent_exit_context(account: dict, *, plans: list[dict] | None = None,
                             contract_size: float | None = None, symbol: str = '') -> str:
    snapshot = account.get('independent_exits')
    local_real = snapshot is None
    units = '标的币数量'
    cycle_notes = []
    if local_real:
        snapshot = {'exits': [], 'uncovered': {'LONG': 0.0, 'SHORT': 0.0}, 'pending': False}
        quantities = {side: sum(float(p.get('amount') or 0) for p in account.get('real_positions', [])
                               if str(p.get('side')).upper() == side) for side in ('LONG', 'SHORT')}
        size_known = isinstance(contract_size, (int, float)) and math.isfinite(contract_size) and contract_size > 0
        size = contract_size if size_known else 1
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
            plan_pending = bool(plan.get('error') or plan.get('state') == 'EXITING'
                                or plan.get('unobserved_entry_fills') or plan.get('cleanup_fill_unseen')
                                or any(r.get('fill_pending') or r.get('replacement')
                                       for r in plan.get('entries', []) + plan.get('exits', [])))
            cycle_notes.append(f"{plan.get('side')} 本任务独立退出周期：{plan.get('state')}；待核验={plan_pending}")
            snapshot['pending'] |= plan_pending
            for record in plan.get('exits', []):
                snapshot['pending'] |= bool(record.get('replacement') or record.get('fill_pending'))
                status = str(record.get('status') or 'unknown').lower()
                if status in {'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'expired'}:
                    continue
                remaining = max(float(record.get('amount') or 0) - float(record.get('filled') or 0), 0) * size
                pending = plan_pending or not record.get('id') or status != 'open'
                snapshot['exits'].append({**record, 'pos_side': plan.get('side'), 'remaining': remaining, 'pending': pending})
                snapshot['pending'] |= pending
                if size_known and record.get('exit_type') == 'stop_market' and not pending:
                    side = plan.get('side')
                    if side in quantities:
                        quantities[side] = max(0, quantities[side] - remaining)
        snapshot['uncovered'] = quantities if not coverage_unknown else {'LONG': None, 'SHORT': None}
        if not cycles_readable:
            snapshot['pending'] = True
            cycle_notes.append('持仓或本任务周期记录读取不完整，归属未知；先查询核验，不得假设可以提交退出单。')
        else:
            for side in sorted(visible_sides - managed_sides):
                snapshot['pending'] = True
                cycle_notes.append(f'{side} 可见交易所持仓，但本任务没有活跃独立退出周期。先核对 config_id、symbol、持仓方向和原开仓任务；不能将手动仓或其他任务持仓自动认领，也不要用新增开仓修复周期。')
    lines = [f'独立退出单（{units}；' + ('本地核验记录非实时成交证明' if local_real else '模拟账户快照') + '）。附带 TP/SL 为空不代表没有独立退出单。']
    lines.extend(cycle_notes)
    for order in snapshot.get('exits', []):
        lines.append(
            f"ID={order.get('order_id') or order.get('id') or order.get('client_id') or '未确认'} | "
            f"{order.get('pos_side')} | {order.get('exit_type')} | price={order.get('price')} | "
            f"trigger={order.get('trigger_price')} | remaining={order.get('remaining')} | "
            f"status={order.get('status')} | pending={bool(order.get('pending'))}"
        )
    if not snapshot.get('exits'):
        lines.append('本系统快照未见活跃独立退出单；不能据此排除交易所其他来源的挂单。')
    coverage = snapshot.get('uncovered') or {}
    lines.append('本系统独立 SL 未覆盖数量：' + '；'.join(f"{side}={coverage.get(side) if coverage.get(side) is not None else '未知'}" for side in ('LONG', 'SHORT')))
    lines.append(f"待核验={bool(snapshot.get('pending'))}；待核验时不得把本地订单当作已生效保护或重复提交。")
    return '\n'.join(lines)
