"""Model-only history projection; original messages and execution evidence stay intact."""
from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, ToolMessage

from backend.utils.trade_summary import compact_trade_receipt


RETIRED_QUERY_TOOLS = frozenset({'get_trade_operation', 'get_operation', 'get_paper_operation'})
_RETIRED_SENTENCE = re.compile(
    r'[^\n。！？.!?]*(?:get_trade_operation|get_operation|get_paper_operation)[^\n。！？.!?]*[。！？.!?]?', re.I)


def project_generated_text(value: str) -> str:
    """Retired framework instructions in generated history cannot re-enter a prompt."""
    return _RETIRED_SENTENCE.sub('Execution evidence is verified by the system automatically.', value)


def _generated_content(content):
    if isinstance(content,str):
        return project_generated_text(content)
    if not isinstance(content,list):
        return content
    projected=[]
    for block in content:
        if isinstance(block,str):
            projected.append(project_generated_text(block))
        elif isinstance(block,dict):
            if block.get('type') in {'tool_use','function_call'} and block.get('name') in RETIRED_QUERY_TOOLS:
                continue
            if isinstance(block.get('text'),str):
                projected.append({**block,'text':project_generated_text(block['text'])})
            else:
                projected.append(block)
        else:
            projected.append(block)
    return projected


def project_trade_messages(messages: list, config_id: str | None = None) -> list:
    """Keep financial facts, remove obsolete query call pairs, never edit checkpoints."""
    retired_ids = {
        call.get('id') for message in messages if isinstance(message, AIMessage)
        for call in message.tool_calls if call.get('name') in RETIRED_QUERY_TOOLS
    }
    projected = []
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id in retired_ids or message.name in RETIRED_QUERY_TOOLS:
                continue
            try:
                value = json.loads(message.content) if isinstance(message.content, str) else message.content
            except (ValueError, TypeError):
                value = None
            if isinstance(value, dict) and any(key in value for key in (
                    'operation_id', 'receipt_version', 'contract_version', 'execution', 'order_id')):
                if config_id:
                    from backend.utils.trade_recovery import current_receipt_projection
                    value = current_receipt_projection(config_id, value.get('operation_id') or message.tool_call_id, value)
                else:
                    value = compact_trade_receipt(value)
                content = json.dumps(value, ensure_ascii=False)
                projected.append(message.model_copy(update={'content': content}))
            else:
                content = _generated_content(message.content)
                projected.append(message.model_copy(update={'content': content}))
        elif isinstance(message, AIMessage):
            calls = [call for call in message.tool_calls if call.get('name') not in RETIRED_QUERY_TOOLS]
            content = _generated_content(message.content)
            if calls or content or not message.tool_calls:
                # Provider-specific serialized tool_calls could otherwise restore
                # a removed call even after the normalized list was filtered.
                extra = dict(message.additional_kwargs)
                if len(calls) != len(message.tool_calls):
                    extra.pop('tool_calls', None)
                    extra.pop('function_call', None)
                projected.append(message.model_copy(update={
                    'content': content, 'tool_calls': calls, 'additional_kwargs': extra}))
        else:
            projected.append(message)
    return projected
