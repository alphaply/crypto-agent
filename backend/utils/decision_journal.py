"""A bounded local decision excerpt; no additional model call or invented summary."""
from langchain_core.messages import AIMessage, ToolMessage
from collections import Counter

from backend.utils.llm_utils import extract_message_text
from backend.utils.trade_operations import tool_result_status


def _excerpt(text: str, limit: int) -> str:
    text = str(text or '').strip()
    return text if len(text) <= limit else text[:limit] + '…（原文截取，非完整总结）'


def decision_journal(messages: list, fallback: str = '') -> str:
    finals = [extract_message_text(msg) for msg in messages
              if isinstance(msg, AIMessage) and not msg.tool_calls and extract_message_text(msg).strip()]
    names = {call['id']: call['name'] for msg in messages if isinstance(msg, AIMessage)
             for call in (msg.tool_calls or [])}
    receipts = [msg for msg in messages if isinstance(msg, ToolMessage)]
    lines = []
    if receipts:
        counts = Counter(tool_result_status(extract_message_text(msg)) for msg in receipts)
        lines.append('【执行状态（非成交证明）】' + ', '.join(f'{status}={count}' for status, count in sorted(counts.items())))
        # Keep an unresolved receipt ahead of narrative. The current account and
        # ledger remain authoritative; the full trace is available separately.
        unresolved = [msg for msg in receipts if tool_result_status(extract_message_text(msg)) in {'failed', 'unknown', 'pending'}]
        msg = (unresolved or receipts)[-1]
        name = msg.name or names.get(msg.tool_call_id, 'tool')
        lines.append(_excerpt(f'{name} [{msg.tool_call_id}]: {extract_message_text(msg)}', 130))
    else:
        lines.append('【执行】本轮无工具调用；已有持仓和挂单以最新账户为准。')
    lines.append('【决策摘录】' + _excerpt(finals[-1] if finals else fallback, 250))
    return _excerpt('\n'.join(lines), 475)
