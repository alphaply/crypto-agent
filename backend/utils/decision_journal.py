"""Persist complete decision text and tool receipts without another model call."""
from langchain_core.messages import AIMessage, ToolMessage
from collections import Counter

from backend.utils.llm_utils import extract_message_text
from backend.utils.trade_operations import tool_result_status


def decision_journal(messages: list, fallback: str = '') -> str:
    decisions = [extract_message_text(msg) for msg in messages
                 if isinstance(msg, AIMessage) and extract_message_text(msg).strip()]
    names = {call['id']: call['name'] for msg in messages if isinstance(msg, AIMessage)
             for call in (msg.tool_calls or [])}
    receipts = [msg for msg in messages if isinstance(msg, ToolMessage)]
    lines = []
    if receipts:
        counts = Counter(tool_result_status(extract_message_text(msg)) for msg in receipts)
        lines.append('【执行状态（非成交证明）】' + ', '.join(f'{status}={count}' for status, count in sorted(counts.items())))
        for msg in receipts:
            name = msg.name or names.get(msg.tool_call_id, 'tool')
            lines.append(f'{name} [{msg.tool_call_id}]: {extract_message_text(msg)}')
    else:
        lines.append('【执行】本轮无工具调用；已有持仓和挂单以最新账户为准。')
    lines.append('【决策原文】\n' + '\n\n'.join(decisions or [str(fallback or '')]))
    return '\n'.join(lines)
