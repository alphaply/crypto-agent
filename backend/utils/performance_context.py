"""Read-only performance evidence for scheduled and task-bound chat decisions."""

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


def performance_context(config_id: str, symbol: str, mode: str) -> str:
    """Never fall back to another agent or treat deposits as strategy alpha."""
    mode = mode.upper()
    if mode not in {'REAL', 'STRATEGY'}:
        return ''
    lines = ['## Agent 收益与回撤（本地历史快照，非实时账户余额）']
    table = 'balance_history' if mode == 'REAL' else 'mock_balance_history'
    try:
        with database.get_db_conn() as conn:
            rows = conn.execute(
                f'SELECT timestamp, total_equity FROM {table} '
                'WHERE config_id=? AND symbol=? ORDER BY timestamp, id',
                (config_id, symbol),
            )
            metrics = equity_metrics(rows)
        if metrics['available']:
            lines.extend([
                f"统计起点: {metrics['start']['timestamp']} | 起点权益: {metrics['start']['equity']:.2f} USDT",
                f"截至快照: {metrics['latest']['timestamp']} | 权益: {metrics['latest']['equity']:.2f} USDT | 样本: {metrics['samples']}",
                f"起点收益率: {metrics['return_pct']:+.2f}% | 当前回撤: {metrics['drawdown_pct']:.2f}% | 快照最大回撤: {metrics['max_drawdown_pct']:.2f}%",
            ])
        else:
            lines.append('起点收益率/回撤: N/A（不足两个有效权益快照，不能视为零收益）')
    except Exception:
        lines.append('起点收益率/回撤: N/A（历史快照读取失败）')
    lines.append('起点是本配置、本品种最早有效权益快照，不一定是启用日期；与图表所选窗口起点可能不同。未剔除出入金、划转或共享账户其他策略影响，不能归因为本 Agent 的独立收益；快照可能滞后且会漏掉盘中回撤。')
    try:
        closed = database.get_closed_positions_7d(config_id, symbol=symbol, days=7, mode=mode)
        lines.append(database.format_closed_positions_summary(closed, days=7))
    except Exception:
        lines.append('7天平仓统计: N/A（读取失败，不代表没有交易）')
    lines.append('决策参考：结合收益、回撤、完整平仓样本数与盈亏比评估表现；历史胜率不代表下笔胜率。亏损不是加杠杆、放宽止损或强行回本的理由。')
    return '\n'.join(lines)
