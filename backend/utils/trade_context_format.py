"""Compact, deterministic display of decision facts; no I/O or trading decisions."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pytz


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _number(value):
    number = _decimal(value)
    if number is None:
        return '未知'
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _text(value):
    return str(value if value is not None else '未知').replace('\n', ' ').replace('\r', ' ').replace('|', '\\|')


def _side(value):
    return {'LONG': '多仓', 'SHORT': '空仓'}.get(str(value).upper(), f'方向未核验({_text(value)})')


def _ownership(value):
    return {'task': '本任务', 'unverified': '归属未核验', 'external': '外部仓位/订单'}.get(value, f'归属：{_text(value)}')


def _management(row):
    facts = row.get('management') or {}
    state = row.get('management_state') or facts.get('state')
    names = {'owned': '已纳管', 'eligible_unclaimed': '可自动认领', 'conflict': '其他管理者占用',
             'read_only': '只读可见', 'needs_refresh': '管理证据待核验'}
    if not state:
        return ''
    owner = row.get('owner') or facts.get('owner')
    return names.get(state, _text(state)) + (f'；管理者 {_text(owner)}' if owner else '')


def _observations(rows, sources=None):
    groups = {}
    for row in rows:
        groups.setdefault(row.get('source', 'unknown'), []).append(row.get('observed_at'))
        if 'snapshot_observed_at' in row:
            groups.setdefault('exchange_snapshot', []).append(row['snapshot_observed_at'])
    for name, source in (sources or {}).items():
        if isinstance(source, dict):
            groups.setdefault(name, []).append(source.get('observed_at'))
    labels = {'exchange_snapshot': '交易所快照', 'mock_snapshot': '模拟快照',
              'local_execution_evidence': '本地核验', 'unknown': '来源未知',
              'positions': '持仓快照', 'orders': '普通单快照', 'trigger_orders': '条件单快照',
              'agent': 'Agent', 'external': '外部', 'baseline': '接管基线'}
    parts = []
    for source, values in sorted(groups.items()):
        times, unknown = [], False
        for value in values:
            try:
                if value is None:
                    raise ValueError('No observation time')
                times.append(datetime.fromtimestamp(float(value), pytz.timezone('Asia/Shanghai')))
            except (ValueError, TypeError, OverflowError, OSError):
                unknown = True
        stamp = '未知'
        if times:
            first, last = min(times), max(times)
            stamp = first.strftime('%Y-%m-%d %H:%M:%S')
            last_stamp = last.strftime('%Y-%m-%d %H:%M:%S')
            if stamp != last_stamp:
                stamp += '～' + last_stamp
            if unknown:
                stamp += '；部分未知'
        parts.append(f'{labels.get(source, _text(source))} {stamp}')
    return '观察时间（UTC+8）：' + '；'.join(parts) if parts else '观察时间：未知'


def render_positions(state):
    lines = []
    currency = str(state.scope.get('symbol') or '计价币').split(':')[-1].split('/')[-1]
    for position in state.positions:
        amount = _number(position.get('amount'))
        if position.get('last_known_amount') is not None:
            amount += f"（最后已知 {_number(position['last_known_amount'])}）"
        pnl = _decimal(position.get('unrealized_pnl'))
        pnl_text = f'约 {pnl:+.2f} {currency}' if pnl is not None else '未知'
        owner = _ownership(position.get('ownership')) + ('（已接管）' if position.get('adoption') else '')
        owner = _management(position) or owner
        lines.append(f"{_side(position.get('side'))} {amount}｜均价 {_number(position.get('entry_price'))}｜浮盈{pnl_text}｜{owner}")
    if not lines:
        lines.append('未知（持仓读取不完整）' if state.evidence_state.get('positions') == 'unavailable' else '无')
    sources = state.evidence_state.get('sources', {})
    return '\n'.join([*lines, _observations(state.positions, {k: v for k, v in sources.items() if k == 'positions'})])


def _purpose(order):
    kind, role = order.get('order_type'), order.get('role')
    names = {'stop_market': '止损市价', 'stop_limit': '止损限价',
             'take_profit_market': '止盈市价', 'take_profit_limit': '止盈限价'}
    if role == 'entry':
        purpose = {'limit': '限价入场', 'market': '市价入场'}.get(kind, f'{_text(kind)}入场')
    else:
        purpose = names.get(kind, {'limit': '限价退出', 'market': '市价退出'}.get(kind, _text(kind)))
    if role not in {'entry', 'exit'}:
        purpose = f"{_text(kind)}（用途未核验）"
    return _side(order.get('side')) + purpose


def _order_row(order):
    identity = order.get('order_id') or ('client:' + str(order['client_id']) if order.get('client_id') else '身份未知')
    quantity = '全部（触发时该方向）' if order.get('quantity_mode') == 'all' else _number(order.get('remaining'))
    if order.get('quantity_mode') == 'all' and order.get('resolved_amount') is not None:
        quantity += '；已确定 ' + _number(order['resolved_amount'])
    kind = order.get('order_type') or ''
    prices = []
    if kind in {'stop_market', 'stop_limit', 'take_profit_market', 'take_profit_limit'} or order.get('trigger_price') is not None:
        prices.append('触发 ' + _number(order.get('trigger_price')))
    if kind in {'limit', 'stop_limit', 'take_profit_limit'} or (order.get('price') is not None and not prices):
        prices.append('限价 ' + _number(order.get('price')))
    statuses = {'open': '挂单中', 'waiting_trigger': '待触发', 'triggered': '已触发', 'partially_filled': '部分成交',
                'partial': '部分成交', 'unknown': '状态未知', 'pending': '待核验', 'submitting': '提交中',
                'filled': '已成交', 'closed': '已结束', 'canceled': '已撤销', 'cancelled': '已撤销'}
    status = statuses.get(str(order.get('status')).lower(), _text(order.get('status')))
    notes = []
    if _management(order):
        notes.append(_management(order))
    if order.get('source') == 'external':
        notes.append('外部来源，已纳管' if order.get('managed') else '外部来源，待核验')
    if order.get('supported_actions'):
        notes.append('支持 ' + '/'.join(order['supported_actions']) + '（执行前核验）')
    if order.get('ownership') != 'task':
        notes.append(_ownership(order.get('ownership')))
    if not order.get('verified'):
        notes.append('事实待核验')
    if order.get('parent_order_id'):
        notes.append('父单 ' + _text(order['parent_order_id']))
    # Show differing snapshot facts once, without repeating identical metadata.
    for field, baseline, label in [('snapshot_remaining', 'remaining', '快照余量'),
                                   ('snapshot_trigger_price', 'trigger_price', '快照触发')]:
        if order.get(field) is not None and _decimal(order[field]) != _decimal(order.get(baseline)):
            notes.append(label + ' ' + _number(order[field]))
    comparison_price = order.get('price') if order.get('price') is not None else order.get('trigger_price')
    if order.get('snapshot_price') is not None and _decimal(order['snapshot_price']) != _decimal(comparison_price):
        notes.append('快照价格 ' + _number(order['snapshot_price']))
    snapshot_status = str(order.get('snapshot_status') or '').lower()
    local_status = str(order.get('status') or '').lower()
    if snapshot_status and snapshot_status != local_status and {snapshot_status, local_status} != {'open', 'waiting_trigger'}:
        notes.append('快照状态 ' + statuses.get(snapshot_status, _text(snapshot_status)))
    if notes:
        status += '；' + '；'.join(notes)
    return f"{_text(identity)} | {_purpose(order)} | {quantity} | {'；'.join(prices) or ('市价' if kind == 'market' else '未知')} | {status}"


_REASONS = {
    'account_snapshot_unavailable': '账户快照不可用', 'positions_unavailable': '持仓读取失败',
    'orders_unavailable': '普通单读取失败', 'trigger_orders_unavailable': '条件单读取失败',
    'cycle_evidence_unavailable': '任务仓位/订单证据不可用', 'ownership_unverified': '仓位归属未核验',
    'ownership_error': '仓位归属冲突', 'position_error': '仓位证据冲突',
    'order_unresolved': '订单结果未核验', 'fill_unverified': '成交明细未核验',
    'execution_unverified': '执行结果未核验', 'cycle_cleanup_unverified': '退出清理尚未核验',
    'residual_position_requires_management': '仍有剩余仓位需管理',
    'order_quantity_conflict': '订单余量冲突', 'order_snapshot_conflict': '快照未找到本地有效单',
    'referenced_evidence_unavailable': '原操作证据读取失败', 'referenced_evidence_missing': '原操作证据缺失',
    'referenced_evidence_scope_mismatch': '原操作账户/标的不匹配',
}
_MISSING = {
    'real_positions': '交易所持仓', 'real_open_orders': '交易所挂单',
    'current_task_ownership_and_orders': '当前任务归属及订单', 'current_task_position_ownership': '当前任务仓位归属',
    'verified_execution_outcome': '已核验执行结果', 'position_and_cleanup_fills': '仓位及清理成交',
    'reconcile_snapshot_remaining_with_local_fill': '快照余量与本地成交对齐',
    'live_order_in_current_snapshot': '当前快照中的活动订单',
    'original_operation_in_current_account': '当前账户中的原操作证据',
}


def _issue_text(issue):
    reason = issue.get('reason_code')
    parts = [_REASONS.get(reason, _text(reason))]
    if issue.get('side'):
        parts.append(_side(issue['side']))
    scopes = {'same_side': '同方向', 'shared_net_position': '共享净仓', 'position_mode_unverified': '持仓模式未核验'}
    if issue.get('scope'):
        parts.append(scopes.get(issue['scope'], _text(issue['scope'])))
    for key, label in [('order_id', '订单'), ('operation_id', '操作'), ('episode_id', '周期')]:
        if issue.get(key):
            parts.append(label + ' ' + _text(issue[key]))
    if issue.get('affected_effects'):
        effects = {'increase_exposure': '增加敞口', 'reduce_exposure': '减少敞口'}
        parts.append('涉及 ' + '/'.join(effects.get(e, _text(e)) for e in issue['affected_effects']))
    if issue.get('missing_evidence'):
        missing = issue['missing_evidence']
        parts.append('缺少 ' + _MISSING.get(str(missing), _text(missing)))
    for key, label in [('amount', '剩余量'), ('last_known_amount', '最后已知量')]:
        if issue.get(key) is not None:
            parts.append(label + ' ' + _number(issue[key]))
    if issue.get('last_error'):
        parts.append(_text(issue['last_error']))
    return '；'.join(parts)


def render_orders(state):
    sources = state.evidence_state.get('sources', {})
    incomplete = (state.evidence_state.get('local_cycles') == 'unavailable'
                  or state.evidence_state.get('positions') == 'unavailable'
                  or any(isinstance(value, dict) and value.get('error') for value in sources.values()))
    lines = ['当前挂单（数量单位：标的币；本任务订单，其他归属单列）']
    if state.orders:
        lines += ['ID | 用途 | 数量 | 价格 | 状态', '--- | --- | --- | --- | ---']
        lines += [_order_row(order) for order in state.orders]
    else:
        lines.append('挂单未知（存在缺项）。' if incomplete else
                     '尚无已确认挂单（存在缺项，见异常）。' if state.issues else '无挂单。')
    lines.append(_observations(state.orders, {k: v for k, v in sources.items() if k in {'orders', 'trigger_orders'}}))
    uncovered = state.coverage.get('uncovered', {})
    lines.append('止损未覆盖估计：' + ('；'.join(f'{_side(side)} {_number(value)}' for side, value in uncovered.items()) or '未知'))
    for conflict in state.coverage.get('quantity_conflict', []):
        group = {'stop_loss': '止损', 'take_profit': '止盈'}.get(conflict.get('group'), _text(conflict.get('group')))
        lines.append(f"数量冲突：{_side(conflict.get('side'))}{group}固定余量 {_number(conflict.get('fixed_remaining'))} > 持仓 {_number(conflict.get('position_amount'))}")
    if any(order.get('order_type') in {'stop_limit', 'take_profit_limit'} for order in state.orders):
        lines.append('条件限价单触发后仍须限价成交，不能视为已退出。')
    lines.append('当前相关执行异常：' + ('\n' + '\n'.join('- ' + _issue_text(issue) for issue in state.issues)
                                         if state.issues else '无法判断（数据读取不完整）。' if incomplete else '无（当前数据源读取完整）。'))
    if state.issues:
        lines.append(_observations([dict(source='local_execution_evidence', observed_at=i.get('observed_at')) for i in state.issues]))
    lines.append('挂单不保证成交；执行层在动作前核验，历史建议不构成本轮许可或封锁。')
    return '\n'.join(lines)
