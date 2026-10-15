"""Exercise real prompt construction, correction and audit without network calls."""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.agent import memory_agent as memory, context_projections as projections
from backend.utils import llm_utils
from test_context_projections import CFG, LEGACY, THESIS, sources, simulation

START_AUDIT, FINISH_AUDIT = memory._start_audit, memory._finish_audit


def response(value, **metadata):
    return AIMessage(content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                     response_metadata=metadata,
                     usage_metadata={'input_tokens': 12, 'output_tokens': 8, 'total_tokens': 20})


def scripted_model(monkeypatch, *outputs):
    calls, costs, audits = [], [], []
    def invoke(messages):
        calls.append(deepcopy(messages))
        item = outputs[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(memory, 'build_chat_model', lambda **_: Mock(invoke=invoke))
    monkeypatch.setattr(memory.database, 'save_token_usage', lambda **kw: costs.append(kw))
    monkeypatch.setattr(memory, '_start_audit', lambda *a: f'call-{len(calls) + 1}')
    monkeypatch.setattr(memory, '_finish_audit', lambda identity, **kw: audits.append((identity, deepcopy(kw))) if identity else None)
    return calls, costs, audits


@pytest.mark.parametrize('role', ['system', 'user'])
def test_one_plain_text_contract_with_legacy_custom_prompt(sources, monkeypatch, role):
    calls, costs, audits = scripted_model(monkeypatch, response(THESIS))
    result = memory.run_memory_review('本轮压缩及旧记忆', {**CFG, 'system_prompt_role': role,
                                     'short_memory_prompt': '旧格式 {"summary":"..."} {content}'}, operation_id='plain')
    assert result.summary == THESIS and result.status == 'completed'
    assert len(calls) == len(costs) == 1
    assert '本轮压缩及旧记忆' in calls[0][-1].content
    assert '纯文本' in calls[0][0].content
    assert 'source_hash' not in str(calls[0])
    assert audits[-1][1]['status'] == 'success'


@pytest.mark.parametrize('text,metadata', [('', {}), ('完整文本', {'finish_reason': 'length'})])
def test_invalid_response_is_not_corrected(sources, monkeypatch, text, metadata):
    calls, costs, audits = scripted_model(monkeypatch, response(text, **metadata))
    result = memory.run_memory_review('完整来源', CFG, operation_id='invalid')
    assert result.status == 'failed' and len(calls) == 1
    assert audits[-1][1]['status'] == 'error'
