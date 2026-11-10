"""Compact, deterministic display of decision facts; no I/O or trading decisions."""
from decimal import Decimal, InvalidOperation


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
    return {'LONG': '多头', 'SHORT': '空头'}.get(str(value).upper(), f'方向未核验({_text(value)})')


def _ownership(value):
    return {'task': '本任务', 'unverified': '归属未核验', 'external': '外部仓位/订单'}.get(value, f'归属：{_text(value)}')


def _management(row):
    facts = row.get('management') or {}
    state = row.get('management_state') or facts.get('state')
    names = {'shared': '授权共管', 'owned': '已纳管', 'eligible_unclaimed': '可自动认领', 'conflict': '其他管理者占用',
             'read_only': '只读可见', 'needs_refresh': '管理证据待核验'}
    if not state:
        return ''
    return names.get(state, _text(state))


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
    if state.evidence_state.get('positions') == 'unavailable':
        lines.append('持仓未知（读取不完整）')
    return '\n'.join(lines) or '无'


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
        notes.append('支持 ' + '/'.join(order['supported_actions']))
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


def render_orders(state):
    lines = ['当前挂单（数量单位：标的币；本任务订单，其他归属单列）']
    if state.orders:
        lines += ['ID | 用途 | 数量 | 价格 | 状态', '--- | --- | --- | --- | ---']
        lines += [_order_row(order) for order in state.orders]
    if state.evidence_state.get('orders') == 'unavailable' or state.evidence_state.get('local_cycles') == 'unavailable':
        lines.append('挂单数据不完整。')
    elif not state.orders:
        lines.append('尚无已确认挂单。' if state.issues else '无挂单。')
    for conflict in state.coverage.get('quantity_conflict', []):
        group = {'stop_loss': '止损', 'take_profit': '止盈'}.get(conflict.get('group'), _text(conflict.get('group')))
        lines.append(f"数量冲突：{_side(conflict.get('side'))}{group}固定余量 {_number(conflict.get('fixed_remaining'))} > 持仓 {_number(conflict.get('position_amount'))}")
    return '\n'.join(lines)
