"""Persist model output and tool receipts directly, without a formatting model."""
import json

from langchain_core.messages import AIMessage, ToolMessage

from backend.database.agent_runs import _json_value
from backend.utils.llm_utils import extract_message_text, extract_reasoning_content, extract_reasoning_token_count
from backend.utils.trade_operations import tool_result_status


def build_decision_record(messages: list, summary_status: str) -> dict:
    calls = {call['id']: call for msg in messages if isinstance(msg, AIMessage)
             for call in (msg.tool_calls or [])}
    entries = []
    receipts = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            entries.append({
                'role': 'assistant', 'content': extract_message_text(msg),
                'reasoning_content': extract_reasoning_content(msg),
                'reasoning_tokens': extract_reasoning_token_count(msg),
                'reasoning_diagnostics': msg.additional_kwargs.get('reasoning_diagnostics'),
                'tool_calls': _json_value(msg.tool_calls or []),
                'output_warning': msg.additional_kwargs.get('output_warning', ''),
            })
        elif isinstance(msg, ToolMessage):
            result = extract_message_text(msg)
            receipt = {'tool': msg.name or calls.get(msg.tool_call_id, {}).get('name', 'tool'),
                       'tool_call_id': msg.tool_call_id, 'result': result,
                       'status': tool_result_status(result)}
            if receipt['tool'] == 'get_trade_operation':
                try:
                    receipt['status'] = json.loads(result).get('query', {}).get('status', receipt['status'])
                except (TypeError, ValueError, AttributeError):
                    pass
            receipts.append(receipt)
            entries.append({'role': 'tool', **receipt})
    return {'version': 1, 'messages': entries, 'execution_results': receipts, 'summary_status': summary_status}


def read_decision_record(summary: dict) -> dict | None:
    """Old reports are read-only evidence; never regenerate them during migration."""
    raw = summary.get('decision_json')
    if raw:
        try:
            record = json.loads(raw)
            if isinstance(record, dict) and isinstance(record.get('messages'), list):
                from backend.utils.trade_recovery import latest_operation_receipts
                latest = latest_operation_receipts(summary.get('config_id'), summary.get('symbol', ''))
                from backend.utils.trade_summary import compact_trade_receipt
                from backend.utils.trade_operations import _decode_receipt
                from urllib.parse import quote
                for entry in record['messages']:
                    if entry.get('role') != 'tool':
                        continue
                    original = _decode_receipt(entry.get('result'))
                    identity = original.get('operation_id') if isinstance(original, dict) else None
                    identity = identity or entry.get('tool_call_id')
                    if isinstance(original, dict):
                        entry['result'] = compact_trade_receipt(original)
                    if identity in latest:
                        entry['current_receipt'] = compact_trade_receipt(latest[identity])
                    if identity and summary.get('config_id') and entry.get('tool') not in {'get_operation', 'get_trade_operation'}:
                        entry['audit'] = {'url': '/trade-receipts/' + quote(str(summary['config_id']), safe='') + '/' + quote(str(identity), safe='') + '/audit'}
                for entry in record.get('execution_results', []):
                    if not isinstance(entry, dict):
                        continue
                    value = _decode_receipt(entry.get('result'))
                    if isinstance(value, dict):
                        entry['result'] = compact_trade_receipt(value)
                return record
        except (ValueError, TypeError):
            pass
        return {'messages': [], 'execution_results': [], 'unavailable': True}
    if not summary.get('report_json'):
        return None
    try:
        report = json.loads(summary['report_json'])
        if not isinstance(report, dict):
            return None
        receipts = report.get('execution_results') or []
        return {
            'legacy': True, 'raw_analysis': report.get('raw_analysis'),
            'messages': [], 'execution_results': receipts,
            'summary_status': 'failed' if report.get('validation_status') == 'invalid' else 'completed',
        }
    except (ValueError, TypeError):
        return {'messages': [], 'execution_results': [], 'unavailable': True}


def is_transient_schema_error(result: str | None) -> bool:
    if not result or not isinstance(result, str):
        return False
    indicators = (
        'validation error for',
        'pydantic.dev',
        'Input should be',
        'Value error, depends_on requires',
        'ValidationError',
    )
    return any(ind in result for ind in indicators)


def compact_action_payload(item: dict) -> dict:
    if not isinstance(item, dict):
        return item
    compact = {}
    for k in (
        'action', 'status', 'side', 'pos_side', 'amount', 'filled_amount',
        'remaining_amount', 'actual_remaining', 'requested_amount', 'price',
        'trigger_price', 'exit_type', 'order_type', 'order_id', 'exchange_order_id',
        'order_status', 'edit_method', 'previous_order_id', 'protection_state',
        'pending', 'operation_id'
    ):
        if k in item and item[k] is not None:
            compact[k] = item[k]
    pos = item.get('position')
    if isinstance(pos, dict) and pos.get('amount') is not None:
        compact['position'] = {'side': pos.get('side'), 'amount': pos.get('amount'), 'status': pos.get('status')}
    err = item.get('error') or (item.get('cleanup') or {}).get('error')
    if err:
        compact['error'] = err
    return compact


def compact_receipt_payload(payload):
    from backend.utils.trade_operations import _decode_receipt
    data = _decode_receipt(payload)
    if not isinstance(data, dict):
        return payload
    if 'results' in data and isinstance(data['results'], list):
        compact_batch = {
            'status': data.get('status'),
            'operation_id': data.get('operation_id'),
            'execution_policy': data.get('execution_policy'),
            'results': [
                {
                    'index': r.get('index'),
                    'action': r.get('action'),
                    'status': r.get('status'),
                    'result': compact_action_payload(r.get('result', {})),
                }
                for r in data['results'] if isinstance(r, dict)
            ]
        }
        if 'summary' in data:
            compact_batch['summary'] = data['summary']
        return compact_batch
    return compact_action_payload(data)


def compact_evidence_payload(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    compact = {
        'resolution': evidence.get('resolution'),
        'operation_id': evidence.get('operation_id'),
    }
    if 'parent_operation_id' in evidence:
        compact['parent_operation_id'] = evidence['parent_operation_id']
    if 'latest' in evidence:
        compact['latest'] = compact_receipt_payload(evidence['latest'])
    if 'error' in evidence:
        compact['error'] = evidence['error']
    return compact


def compact_execution_receipt(item: dict) -> dict:
    if not isinstance(item, dict):
        return item
    compact = {
        'tool': item.get('tool'),
        'tool_call_id': item.get('tool_call_id'),
        'status': item.get('status'),
    }
    raw_res = item.get('result')
    if is_transient_schema_error(raw_res):
        compact['status'] = 'failed'
        compact['error'] = 'parameter_validation_failed'
        return compact

    compact_res = compact_receipt_payload(raw_res)
    compact['result'] = json.dumps(compact_res, ensure_ascii=False) if isinstance(compact_res, (dict, list)) else raw_res
    if 'current_evidence' in item:
        compact['current_evidence'] = compact_evidence_payload(item['current_evidence'])
    return compact


def append_execution_receipts(text: str, summary: dict) -> str:
    # The decision JSON remains intact; only semantic strategy reaches memory.
    from backend.agent.strategy_material import strategy_material
    return strategy_material(text)


def memory_execution_evidence(summary: dict, record: dict | None = None) -> list:
    """Consumer overlay only. Never change the persisted decision or first receipt."""
    record = record if record is not None else read_decision_record(summary)
    originals = (record or {}).get('execution_results') or []
    valid_originals = [r for r in originals if isinstance(r, dict) and not is_transient_schema_error(r.get('result'))]
    refs = [execution_identity(r) for r in valid_originals if execution_identity(r)]
    from backend.utils.trade_recovery import operation_evidence
    from backend.utils.trade_evidence import configured_account_scope
    try:
        scope = configured_account_scope(summary.get('config_id'))
        latest = operation_evidence(summary.get('config_id'), summary.get('symbol'), refs, account_scope=scope)
    except Exception:
        latest = {i: {'resolution': 'unavailable', 'operation_id': i} for i in refs}
    return [compact_execution_receipt({**r, 'current_evidence': latest.get(execution_identity(r), {'resolution': 'unresolved'})})
            for r in valid_originals]


def execution_identity(item: dict) -> str | None:
    from backend.utils.trade_operations import _decode_receipt
    if is_transient_schema_error(item.get('result')):
        return None
    result = _decode_receipt(item.get('result'))
    if isinstance(result, dict) and result.get('operation_id'):
        return str(result['operation_id'])
    # A model tool-call ID is not proof of a persisted financial submission.
    return None
