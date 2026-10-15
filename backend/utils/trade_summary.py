"""Factual model receipts; durable original evidence is never replaced."""
from copy import deepcopy
import json

from backend.utils.trade_operations import _decode_receipt, tool_result_status


_IDENTITIES = {'operation_id', 'parent_operation_id', 'execution_operation_id', 'index', 'action',
               'order_id', 'order_ids', 'accepted_order_ids', 'previous_order_id', 'parent_order_id', 'execution_order_id', 'exchange_order_id',
               'client_id', 'client_order_id', 'exit_id', 'oco_group', 'symbol', 'side', 'exchange_side', 'user_id', 'profile_id',
               'resource_id', 'cycle_id', 'episode_id'}
_FACTS = {'contract_version', 'receipt_version', 'status', 'order_status', 'order_type', 'exchange_channel',
          'market_type', 'exchange', 'quantity_mode', 'amount', 'requested_amount', 'resolved_amount', 'filled_amount',
          'remaining_amount', 'closed_amount', 'execution_amount', 'price', 'trigger_price', 'average_price',
          'fee', 'realized_pnl', 'edit_method', 'phase', 'submission_state', 'submitted', 'error_code',
          'reason_code', 'blocked_by_index', 'blocked_by_indices', 'depends_on', 'execution_policy',
          'partial_execution', 'rejected_by_user', 'reservation_change', 'reserved_cash', 'reserved_spot',
          'verified_at', 'observed_at', 'last_known_fill_at', 'last_known_filled_amount', 'pending',
          'stop_loss', 'take_profit', 'protection_state', 'persistence_pending', 'state', 'found',
          'cash_after', 'debt_after', 'borrowed', 'repaid', 'bank_action', 'debt', 'cash', 'margin', 'leverage',
          'enabled', 'created', 'name', 'leaderboard_visible', 'notifications_enabled', 'markets',
          'replacement_status', 'previous_order_status', 'quantity_unit', 'filled_cost', 'avg_fill_price', 'filled_at',
          'adopted', 'protection_created',
          'receipt_updated_at', 'projected_at', 'evidence_source', 'current_evidence_status'}
_RETIRED = ('get_trade_operation', 'get_operation', 'get_paper_operation')


def receipt_bytes(value):
    # The SDK can pretty-print its text result. Budget for that representation,
    # which is at least as large as the default compact HTTP JSON.
    return len(json.dumps(value, ensure_ascii=False, indent=2, default=str).encode('utf-8'))


def _small_text(value, maximum=240):
    if not isinstance(value, str) or any(name in value for name in _RETIRED):
        return None
    encoded = value.encode('utf-8')
    return value if len(encoded) <= maximum else encoded[:maximum-3].decode('utf-8',errors='ignore') + '…'


def _diagnostic_items(items, limit=3):
    unique = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        selected = {key: deepcopy(value) for key, value in item.items()
                    if key in _IDENTITIES | {'status', 'order_status', 'reason_code', 'error_code', 'missing_evidence'} and value is not None}
        identity = tuple(str(selected.get(key) or '') for key in ('order_id', 'client_id', 'operation_id', 'reason_code'))
        unique.setdefault(identity, selected)
    values = list(unique.values())
    return values[:limit], len(values)


def _paper_order(order):
    """Paper orders use state/position_side and store quantities in base units."""
    if not isinstance(order, dict):
        return None
    fields = _IDENTITIES | {'order_type', 'market_type', 'exchange', 'price', 'trigger_price',
                            'filled_amount', 'filled_at', 'created_at', 'remaining_amount'}
    result = {key: deepcopy(item) for key, item in order.items() if key in fields}
    if 'state' in order:
        result['order_status'] = order['state']
    if 'position_side' in order:
        result['side'] = order['position_side']
    if 'amount' in order:
        result.update(requested_amount=order['amount'], quantity_unit='base',
                      quantity_mode='all' if order.get('close_all') else 'fixed')
    fill = order.get('fill')
    if isinstance(fill, dict) and fill.get('amount') is not None:
        result.update(filled_amount=fill['amount'], average_price=fill.get('price'))
    return result


def _project(value):
    value = _decode_receipt(value)
    if not isinstance(value, dict):
        return {'status': tool_result_status(value), 'message': _small_text(value) or 'Saved legacy execution result',
                'details_omitted': True}
    projected = {key: deepcopy(item) for key, item in value.items() if key in _IDENTITIES | _FACTS}
    order = _paper_order(value.get('order'))
    if order:
        for key, item in order.items():
            projected.setdefault(key, item)
    for key in ('orders', 'protection_orders'):
        if isinstance(value.get(key), list):
            projected[key] = [_paper_order(order) for order in value[key] if isinstance(order, dict)]
    fill = value.get('fill')
    if isinstance(fill, dict):
        for key, item in fill.items():
            if key in _IDENTITIES | {'market_type', 'exchange', 'price', 'fee', 'realized_pnl', 'observed_at'}:
                projected.setdefault(key, deepcopy(item))
        if fill.get('amount') is not None:
            projected.update(filled_amount=fill['amount'], order_status='filled', quantity_unit='base')
    for target, aliases in {'side': ('pos_side',), 'client_id':('client_order_id','clientOrderId'), 'filled_amount': ('filled',),
                            'remaining_amount': ('remaining', 'actual_remaining'),
                            'requested_amount': ('amount',), 'average_price': ('average',)}.items():
        if target not in projected:
            for alias in aliases:
                if alias in value:
                    projected[target] = deepcopy(value[alias])
                    break
    projected = {key:item for key,item in projected.items()
                 if item is not None or key in {'filled_amount','remaining_amount','equity'}}
    if projected.get('last_known_filled_amount') == projected.get('filled_amount') and projected.get('filled_amount') is not None:
        projected.pop('last_known_filled_amount',None)
        projected.pop('last_known_fill_at',None)
    if projected.get('pending') == (projected.get('status') in {'pending','unknown'}):
        projected.pop('pending',None)
    if projected.get('order_id') and str(projected['order_id']).removeprefix('regular:').removeprefix('algo:') == str(projected.get('exchange_order_id')):
        projected.pop('exchange_order_id', None)
    if projected.get('client_id') == projected.get('client_order_id'):
        projected.pop('client_order_id', None)
    for key in ('error', 'message', 'protection_error', 'reason'):
        short = _small_text(value.get(key))
        if short:
            projected[key] = short
    for key, fields in {
        'execution': {'phase', 'submission_state', 'fill_status'},
        'position': {'side', 'amount', 'status', 'observed_at', 'last_known_amount'},
        'query': {'status', 'source', 'observed_at', 'queried_at'},
        'management': {'state', 'owner', 'source', 'claim_id', 'claim_version', 'reason_code'},
        'admission': {'decision', 'allowed', 'reason_code', 'side', 'assessed_at'},
    }.items():
        item = value.get(key)
        if isinstance(item, dict):
            projected[key] = {name: deepcopy(detail) for name, detail in item.items() if name in fields}
    details = value.get('details')
    if isinstance(details, dict):
        projected['details'] = {key:deepcopy(item) for key,item in details.items() if key in {
            'reason_code','symbol','pos_side','side','cycle_state','position_amount','restorable','owner_config_id'}}
    cleanup = value.get('cleanup')
    if isinstance(cleanup, dict):
        projected['cleanup'] = {key: deepcopy(cleanup[key]) for key in (
            'status', 'pending', 'error_code', 'blocking_order_ids') if key in cleanup}
        items, count = _diagnostic_items(cleanup.get('orders') or cleanup.get('blockers'))
        if items:
            projected['cleanup']['orders'] = items
        if count:
            projected['cleanup']['order_count'] = count
        if short_error := _small_text(cleanup.get('error')):
            projected['cleanup']['error'] = short_error
    continuation = value.get('continuation')
    if isinstance(continuation, dict):
        limits = {key: deepcopy(continuation[key]) for key in ('assessment_state', 'reason_code', 'side') if key in continuation}
        blockers, count = _diagnostic_items(continuation.get('blockers') or [blocker for rule in continuation.get('rules', [])
                                                                          for blocker in rule.get('blockers', [])])
        if blockers:
            limits['blockers'] = blockers
            limits['blocker_count'] = count
        if limits:
            projected['continuation'] = limits
    recovery = value.get('recovery')
    if value.get('status') in {'unknown', 'pending', 'submitted'} or isinstance(recovery, dict):
        projected['verification'] = {'state': 'background', 'operation_id': value.get('operation_id')}
        if isinstance(recovery, dict) and recovery.get('blocker_operation_ids'):
            ids = list(dict.fromkeys(recovery['blocker_operation_ids']))
            projected['verification'].update(blocker_operation_ids=ids[:3], blocker_count=len(ids))
    if isinstance(value.get('results'), list):
        projected['results'] = [_project(item) for item in value['results']]
    if 'result' in value:
        projected['result'] = _project(value['result'])
    if isinstance(value.get('summary'), dict):
        projected['summary'] = {key: item for key, item in value['summary'].items() if key in {
            'completed', 'submitted', 'failed', 'unknown', 'pending', 'not_executed', 'skipped'}}
    if isinstance(value.get('account_summary'), dict):
        projected['account_summary'] = {key: deepcopy(item) for key, item in value['account_summary'].items()
                                        if key in {'status', 'cash', 'debt', 'equity', 'balance', 'free', 'used',
                                                   'observed_at', 'generated_at', 'valuation_complete', 'error_code'} }
    projected['details_omitted'] = True
    if value.get('operation_id'):
        projected['audit_reference'] = {'operation_id': value['operation_id']}
    return projected


def compact_trade_receipt(payload):
    """Return a factual projection without a response-size rejection gate."""
    projected = _project(payload)
    children = projected.get('results')
    if isinstance(children, list):
        for child in children:
            child.pop('audit_reference',None)
            child.pop('details_omitted',None)
            if not child.get('verification', {}).get('blocker_operation_ids'):
                child.pop('verification', None)
            if isinstance(child.get('result'), dict):
                if child['result'].get('operation_id') == child.get('operation_id'):
                    child['result'].pop('operation_id', None)
                    child['result'].pop('audit_reference', None)
                if child['result'].get('action') == child.get('action'):
                    child['result'].pop('action', None)
                for field in ('status', 'symbol', 'side'):
                    if field in child and child['result'].get(field) == child[field]:
                        child['result'].pop(field, None)
                for field in ('contract_version', 'receipt_version'):
                    if child['result'].get(field) == projected.get(field):
                        child['result'].pop(field, None)
                if not child['result'].get('verification', {}).get('blocker_operation_ids'):
                    child['result'].pop('verification', None)
                child['result'].pop('details_omitted', None)
                child.pop('audit_reference', None)
    return projected


def render_trade_receipt(payload) -> str:
    """Model text only. Internal consumers retain the structured receipt."""
    from backend.utils.trade_operations import _decode_receipt, tool_result_status
    value = _decode_receipt(payload)
    if not isinstance(value, dict):
        return str(value)
    # MCP and compatibility tools wrap their structured result once more.
    while 'results' not in value and 'result' in value and not value.get('action'):
        nested = _decode_receipt(value['result'])
        if not isinstance(nested, dict):
            break
        value = nested
    rows = value.get('results') if isinstance(value.get('results'), list) else [value]
    lines = []
    for index, row in enumerate(rows):
        receipt = _decode_receipt(row.get('result', row))
        if not isinstance(receipt, dict):
            receipt = {'message': str(receipt)}
        receipt = {**row, **receipt}
        paper_order = _paper_order(receipt.get('order'))
        if paper_order:
            receipt = {**receipt, **paper_order}
        action = row.get('action') or receipt.get('action') or receipt.get('bank_action') or '操作'
        status = row.get('status') or tool_result_status(receipt)
        identity = receipt.get('order_id') or receipt.get('exchange_order_id') or receipt.get('id')
        target = str(identity) if identity else str(receipt.get('symbol') or '')
        order_ids = list(receipt.get('order_ids') or [])
        for field in ('orders', 'protection_orders'):
            for order in receipt.get(field) or []:
                if isinstance(order, dict):
                    order_id = order.get('order_id') or order.get('id')
                    if order_id:
                        order_ids.append(str(order_id))
        if order_ids:
            target = ' '.join(filter(None, [target, '订单 ' + ' / '.join(dict.fromkeys(map(str, order_ids)))]))
        facts = []
        if receipt.get('amount') is None and receipt.get('requested_amount') is not None:
            receipt['amount'] = receipt['requested_amount']
        for key, label in [('side', ''), ('amount', '数量'), ('price', '价格'), ('trigger_price', '触发价'),
                           ('stop_loss', '止损'), ('take_profit', '止盈')]:
            if receipt.get(key) is not None:
                facts.append(label + str(receipt[key]))
        if receipt.get('quantity_mode') == 'all':
            facts.append('全部仓位')
        if receipt.get('filled_amount') is not None:
            facts.append('已成交数量' + str(receipt['filled_amount']))
        if receipt.get('bank_action'):
            facts = [fact for fact in facts if not fact.startswith('已成交数量')]
        error = receipt.get('error') or receipt.get('message') or receipt.get('reason_code') or value.get('error')
        reason = _small_text(error) if error else ''
        if reason and 'Ownership evidence indexing incomplete' in reason:
            reason = '持仓归属证据同步未完成，未提交'
        if status == 'submitted':
            outcome = '提交成功'
        elif status == 'completed':
            outcome = ('订单已成交，无需撤单' if action == 'cancel' and receipt.get('order_status') in {'filled', 'closed'}
                       else '撤单成功' if action == 'cancel' else '成功')
        elif status in {'unknown', 'pending', 'running'}:
            outcome = '请求失败。原因：' + (reason or '未取得确定接口响应') + '；提交结果未知，请勿重复提交'
        elif status == 'not_executed':
            outcome = '未执行。原因：' + (reason or '前序操作失败或结果未确认')
        elif status == 'skipped':
            outcome = '未执行。原因：' + (reason or '无需执行')
        else:
            outcome = '失败。原因：' + (reason or '接口未返回成功结果')
        # Cancel-replace is two effects: never hide the completed cancellation.
        if action == 'edit' and receipt.get('edit_method') == 'cancel_replace':
            previous = receipt.get('previous_order_id')
            if previous:
                target = f'旧单 {previous}' + (f' → 新单 {identity}' if identity and str(identity) != str(previous) else '')
            if receipt.get('previous_order_status') in {'canceled', 'cancelled'}:
                outcome = '撤旧成功；替代下单' + outcome
        if receipt.get('accepted_order_ids') and status in {'failed', 'unknown', 'pending'}:
            outcome += '；已受理保护单：' + ' / '.join(str(item) for item in receipt['accepted_order_ids'])
        line = f"{index + 1}. {action}：{' '.join([target, *facts]).strip()}，{outcome}。"
        lines.append(line)
    if isinstance(value.get('account_summary'), dict):
        lines.append('账户摘要：' + json.dumps(_project(value)['account_summary'], ensure_ascii=False))
    result = '\n'.join(lines)
    return result
