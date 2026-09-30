"""Render independent exits from supplied snapshots without another exchange request."""
from __future__ import annotations

import math


def independent_exit_context(account: dict, *, plans: list[dict] | None = None,
                             contract_size: float | None = None, symbol: str = '') -> str:
    snapshot = account.get('independent_exits')
    local_real = snapshot is None
    units = '标的币数量'
    if local_real:
        snapshot = {'exits': [], 'uncovered': {'LONG': 0.0, 'SHORT': 0.0}, 'pending': False}
        quantities = {side: sum(float(p.get('amount') or 0) for p in account.get('real_positions', [])
                               if str(p.get('side')).upper() == side) for side in ('LONG', 'SHORT')}
        size_known = isinstance(contract_size, (int, float)) and math.isfinite(contract_size) and contract_size > 0
        size = contract_size if size_known else 1
        coverage_unknown = not size_known or bool(account.get('error')) or plans is None
        if not size_known:
            units = '合约张数（合约面值不可用，不换算币数量）'
        canonical = lambda value: str(value or '').upper() if ':' in str(value or '') else str(value or '').upper() + ':' + str(value or '').split('/')[-1].upper()
        for plan in plans or []:
            if symbol and canonical(plan.get('symbol')) != canonical(symbol):
                continue
            if plan.get('read_error'):
                snapshot['pending'] = True
                coverage_unknown = True
                continue
            if plan.get('execution_mode') != 'independent_exits' or plan.get('state') == 'DONE':
                continue
            snapshot['pending'] |= bool(plan.get('error')) or plan.get('state') == 'EXITING'
            for record in plan.get('exits', []):
                status = str(record.get('status') or 'unknown').lower()
                if status in {'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'expired'}:
                    continue
                remaining = max(float(record.get('amount') or 0) - float(record.get('filled') or 0), 0) * size
                pending = not record.get('id') or status != 'open' or bool(record.get('replacement'))
                snapshot['exits'].append({**record, 'pos_side': plan.get('side'), 'remaining': remaining, 'pending': pending})
                snapshot['pending'] |= pending
                if size_known and record.get('exit_type') == 'stop_market' and not pending:
                    side = plan.get('side')
                    if side in quantities:
                        quantities[side] = max(0, quantities[side] - remaining)
        snapshot['uncovered'] = quantities if not coverage_unknown else {'LONG': None, 'SHORT': None}
    lines = [f'独立退出单（{units}；' + ('本地核验记录非实时成交证明' if local_real else '模拟账户快照') + '）。附带 TP/SL 为空不代表没有独立退出单。']
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
