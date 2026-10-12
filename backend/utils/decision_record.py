"""Persist model output and tool receipts directly, without a formatting model."""
import json

from langchain_core.messages import AIMessage, ToolMessage

from backend.database_agent_runs import _json_value
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
                for entry in record['messages']:
                    if entry.get('role') == 'tool' and entry.get('tool_call_id') in latest:
                        entry['current_receipt'] = latest[entry['tool_call_id']]
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


def append_execution_receipts(text: str, summary: dict) -> str:
    record = read_decision_record(summary)
    if record and record.get('execution_results'):
        text += '\n实际工具回执：\n' + json.dumps(record['execution_results'], ensure_ascii=False)
    elif record and record.get('unavailable'):
        text += '\n结构化回执无法读取；执行状态未知。'
    return text
