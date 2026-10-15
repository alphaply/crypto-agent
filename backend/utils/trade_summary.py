"""Bounded model receipts; durable original evidence is never replaced."""
from copy import deepcopy
import json

from backend.utils.trade_operations import _decode_receipt, tool_result_status


SINGLE_RECEIPT_BYTES = 4096
BATCH_RECEIPT_BYTES = 32768
_IDENTITIES = {'operation_id', 'parent_operation_id', 'execution_operation_id', 'index', 'action',
               'order_id', 'order_ids', 'previous_order_id', 'parent_order_id', 'execution_order_id', 'exchange_order_id',
               'client_id', 'client_order_id', 'exit_id', 'oco_group', 'symbol', 'side', 'exchange_side', 'user_id', 'profile_id',
               'resource_id', 'cycle_id', 'episode_id'}
_FACTS = {'contract_version', 'receipt_version', 'status', 'order_status', 'order_type', 'exchange_channel',
          'market_type', 'exchange', 'quantity_mode', 'requested_amount', 'resolved_amount', 'filled_amount',
          'remaining_amount', 'closed_amount', 'execution_amount', 'price', 'trigger_price', 'average_price',
          'fee', 'realized_pnl', 'edit_method', 'phase', 'submission_state', 'submitted', 'error_code',
          'reason_code', 'blocked_by_index', 'blocked_by_indices', 'depends_on', 'execution_policy',
          'partial_execution', 'rejected_by_user', 'reservation_change', 'reserved_cash', 'reserved_spot',
          'verified_at', 'observed_at', 'last_known_fill_at', 'last_known_filled_amount', 'pending',
          'stop_loss', 'take_profit', 'protection_state', 'persistence_pending', 'state', 'found',
          'cash_after', 'debt_after', 'borrowed', 'repaid', 'debt', 'cash', 'margin', 'leverage',
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


def _fit(value, budget):
    result = deepcopy(value)
    if receipt_bytes(result) <= budget:
        return result
    if isinstance(result.get('result'), dict):
        excess = receipt_bytes(result) - budget
        result['result'] = _fit(result['result'], max(384, receipt_bytes(result['result']) - excess - 128))
        if receipt_bytes(result) <= budget:
            return result
    # Omit verbose diagnostic payloads, keeping exact identities and all action
    # outcomes. They remain readable through the authorized immutable audit.
    for key in ('admission', 'position', 'query', 'continuation', 'management', 'message', 'error', 'protection_error', 'account_summary'):
        if key in result:
            result.pop(key)
            if receipt_bytes(result) <= budget:
                return result
    cleanup = result.get('cleanup')
    if isinstance(cleanup, dict) and len(cleanup.get('orders', [])) > 1:
        cleanup['orders'] = cleanup['orders'][:1]
    if receipt_bytes(result) <= budget:
        return result
    # Typed input IDs are bounded before financial effects. If an exchange
    # supplies larger unexpected metadata, keep the complete exact identifiers
    # and status; never slice an ID or serialized JSON to make it look valid.
    essential = {key: item for key, item in result.items() if key in _IDENTITIES | _FACTS | {
        'result', 'results', 'orders', 'protection_orders', 'cleanup', 'verification', 'details_omitted', 'audit_reference'}}
    if receipt_bytes(essential) > budget:
        raise ValueError('Receipt identities exceed the admitted response budget; original evidence is durably retained')
    return essential


def compact_trade_receipt(payload):
    """Return a bounded factual projection without changing the raw object."""
    projected = _project(payload)
    children = projected.get('results')
    if isinstance(children, list):
        # Reserve parent/summary space, then a uniform envelope for every index.
        child_budget = max(512, (BATCH_RECEIPT_BYTES - 4096) // max(len(children), 1))
        for index, child in enumerate(children):
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
                child['result'] = _fit(child['result'], max(384, child_budget - 160))
            children[index] = _fit(child, child_budget)
        return _fit(projected, BATCH_RECEIPT_BYTES)
    if isinstance(projected.get('result'), dict):
        projected['result'] = _fit(projected['result'], SINGLE_RECEIPT_BYTES - 1024)
    return _fit(projected, SINGLE_RECEIPT_BYTES)
