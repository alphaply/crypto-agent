"""Merge local protection evidence into exchange order rows without claiming live coverage."""

import json
import math
from datetime import datetime, timezone


def load_protection_plans(config_id: str) -> list[dict]:
    from backend.database import get_db_conn

    with get_db_conn() as conn:
        rows = conn.execute(
            'SELECT symbol, side, payload FROM real_protection_plans WHERE config_id=? ORDER BY symbol, side',
            (config_id,),
        ).fetchall()
    plans = []
    for row in rows:
        try:
            plan = json.loads(row['payload'])
            if not isinstance(plan, dict):
                raise ValueError('plan must be an object')
            if str(plan.get('state')).upper() == 'DONE':
                continue
            for key in ('legs', 'entries'):
                if not isinstance(plan.get(key, []), list) or any(
                    not isinstance(item, dict) for item in plan.get(key, [])
                ):
                    raise ValueError('invalid protection records')
        except (ValueError, TypeError):
            plan = {'read_error': '保护记录无法解析，不能假设已有保护'}
        plans.append({**plan, 'symbol': row['symbol'], 'side': row['side']})
    return plans


def _symbol_key(symbol: str) -> str:
    symbol = str(symbol or '').upper()
    if ':' not in symbol and '/' in symbol:
        symbol += ':' + symbol.split('/')[-1]
    return symbol


def _ids(record: dict) -> set[str]:
    return {str(record[key]) for key in ('id', 'order_id', 'execution_order_id', 'client_id')
            if record.get(key) is not None and str(record[key]) not in {'', 'None', 'N/A'}}


def _same_price(first, second) -> bool:
    try:
        return math.isfinite(float(first)) and float(first) == float(second) and float(first) > 0
    except (ValueError, TypeError):
        return False


def merge_order_protection(orders: list[dict], lines: list[str], plans: list[dict], symbol: str) -> str:
    """Attach each same-side plan once; retain unmatched records as explicitly local facts."""
    lines = list(lines)
    for plan in plans:
        plan_symbol, plan_side = plan.get('symbol', symbol), str(plan.get('side') or 'UNKNOWN').upper()
        record_ids = {identifier for record in plan.get('entries', []) + plan.get('legs', [])
                      for identifier in _ids(record)}
        matching = [i for i, order in enumerate(orders)
                    if _symbol_key(order.get('symbol') or symbol) == _symbol_key(plan_symbol)
                    and (str(order.get('pos_side') or '').upper() == plan_side
                         or (str(order.get('pos_side') or 'BOTH').upper() in {'BOTH', 'NET'}
                             and bool(_ids(order) & record_ids)))]
        legs = [leg for leg in plan.get('legs', [])
                if str(leg.get('status') or '').lower() not in {'canceled', 'cancelled', 'rejected', 'expired'}]
        covered_targets = set()
        missing_legs = []
        for leg in legs:
            linked = [i for i in matching if _ids(orders[i]) & _ids(leg)]
            kind = str(leg.get('kind') or '?').upper()
            if linked:
                index = linked[0]
                note = f"本地{kind}={leg.get('status') or 'unknown'}"
                if not _same_price(leg.get('trigger_price'), orders[index].get('price')):
                    note += f"@{leg.get('trigger_price', '?')}（与快照价格待核对）"
                lines[index] += ' | ' + note
                target = plan.get('stop_loss' if kind == 'SL' else 'take_profit')
                if kind in {'SL', 'TP'} and _same_price(target, orders[index].get('price')):
                    covered_targets.add(kind)
            else:
                missing_legs.append(f"{kind}@{leg.get('trigger_price', '?')}:{leg.get('status') or 'unknown'}")
        if plan.get('read_error'):
            note = str(plan['read_error'])
        else:
            targets = [f"{kind}={plan.get(key) or '未设置'}" for kind, key in
                       (('SL', 'stop_loss'), ('TP', 'take_profit')) if kind not in covered_targets]
            verified = plan.get('verified_at')
            try:
                stamp = datetime.fromtimestamp(float(verified), timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if verified is not None else '未核验'
            except (ValueError, TypeError, OverflowError, OSError):
                stamp = '核验时间无效'
            note = f"同向本地计划={plan.get('state') or 'UNKNOWN'}"
            if matching and str(orders[matching[0]].get('pos_side') or '').upper() != plan_side:
                note += f"（{plan_side}，订单ID匹配）"
            if targets:
                note += ' | ' + ' '.join(targets)
            note += f" | 最近核验={stamp}"
            if missing_legs:
                note += ' | 本地记录但挂单快照未见=' + ', '.join(missing_legs)
            elif not legs:
                note += ' | 无已记录保护单'
            if plan.get('error'):
                note += f" | 异常={plan['error']}"
            pending = [str(entry.get('id') or entry.get('client_id') or '未知订单')
                       for entry in plan.get('entries', [])
                       if isinstance(entry.get('amendment'), dict) and entry['amendment'].get('state') == 'pending']
            if pending:
                note += f" | 入场改单待确认={','.join(pending)}（不可重复提交）"
        if matching:
            lines[matching[0]] += ' | ' + note
        else:
            lines.append(f"[{plan_symbol} {plan_side}] 无同向挂单快照；{note}（仅本地记录，待核对）")
    if not orders:
        lines.insert(0, '(No Active Orders in snapshot)')
    if not plans:
        lines.append('本地无活跃保护计划；不代表交易所没有保护单。')
    lines.append('本地核验非实时成交证明；同向计划作用于整仓。WAITING=待成交/待安装；ACTIVE且无异常=仅最近核验通过；EXITING=退出清理中；未设置或未核验不得当作已保护。')
    return '\n'.join(lines)
