"""Exercise real prompt construction, correction and audit without network calls."""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend.agent import memory_agent as memory, context_projections as projections
from backend.utils import llm_utils
from test_context_projections import CFG, LEGACY, THESIS, response_for, sources, simulation

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
@pytest.mark.parametrize('empty_sources', [False, True])
def test_one_contract_custom_material_and_empty_sources(sources, monkeypatch, role, empty_sources, tmp_path):
    context = projections.prepare_review('cfg')
    if empty_sources:
        context['sources'] = []
    monkeypatch.setattr(projections, 'prepare_review', lambda _: context)
    prompt = tmp_path / 'custom-memory.txt'
    custom = '旧格式：只输出 {"summary":"历史模板"}\n{content}'
    prompt.write_text(custom, encoding='utf-8')
    calls, _, _ = scripted_model(monkeypatch, response(response_for(context)))
    result = memory.run_memory_review('完整来源末尾', {**CFG, 'short_memory_prompt_file': str(prompt),
                                                       'system_prompt_role': role}, operation_id='contract')
    assert result.status == 'completed' and len(calls) == 1
    assert prompt.read_text(encoding='utf-8') == custom
    joined = '\n'.join(m.content for m in calls[0])
    assert '完整来源末尾' in joined and '历史模板' in joined
    assert '覆盖前面的单 summary' not in joined
    assert memory.SUMMARY_OUTPUT_POLICY not in joined
    assert 'summary、memory、projections 均必填' in joined
    if role == 'user':
        assert len(calls[0]) == 1 and isinstance(calls[0][0], HumanMessage)
        assert joined.rfind('summary、memory、projections 均必填') > joined.index('</整理素材>')
    else:
        assert '历史模板' not in calls[0][0].content
    if empty_sources:
        assert result.projection_metadata['projections'] == [] and result.projection_metadata['memory']['strategy_text'] == THESIS


def test_output_example_uses_actual_required_models():
    example = json.loads(projections.projection_output_policy().split('完整格式示例：\n')[1])
    assert projections.ProjectionReview.model_validate(example).memory.strategy_text
    assert set(projections.ProjectionReview.model_json_schema()['required']) == {'summary', 'memory', 'projections'}


@pytest.mark.parametrize('first', [{'summary': LEGACY}, '{not json', {'summary': LEGACY, 'memory': [], 'projections': []}])
def test_format_correction_uses_snapshot_and_records_each_attempt(sources, monkeypatch, first):
    context = projections.prepare_review('cfg')
    prepare = Mock(return_value=context)
    monkeypatch.setattr(projections, 'prepare_review', prepare)
    calls, costs, audits = scripted_model(monkeypatch, response(first), response(response_for(context)))
    result = memory.run_memory_review('仅此一次的输入证据', CFG, operation_id='correct')
    assert result.status == 'completed' and result.summary == LEGACY
    assert len(calls) == len(costs) == len(audits) == 2
    prepare.assert_called_once_with('cfg')
    assert '上次响应格式校验失败' in calls[1][0].content
    assert calls[1][1].content == calls[0][1].content
    assert calls[1][1].content.count('仅此一次的输入证据') == 1
    assert [a[1]['status'] for a in audits] == ['error', 'success']
    assert [a[1]['details']['phase'] for a in audits] == ['initial', 'format_correction']
    assert [a[1]['details']['attempt'] for a in audits] == [1, 2]
    assert all(a[1]['details']['operation_id'] == 'correct' for a in audits)
    assert audits[0][1]['output'] == response(first).content


@pytest.mark.parametrize('sequence,completed,count', [
    (['network', 'old', 'valid'], True, 3),
    (['old', 'network', 'valid'], True, 3),
    (['network', 'network', 'old'], False, 3),
    (['old', 'old'], False, 2),
    (['network', 'network', 'network'], False, 3),
])
def test_transport_and_correction_share_actual_budget(sources, monkeypatch, sequence, completed, count):
    context = projections.prepare_review('cfg')
    outputs = {'network': ConnectionError('offline'), 'old': response({'summary': LEGACY}),
               'valid': response(response_for(context))}
    calls, _, _ = scripted_model(monkeypatch, *(outputs[key] for key in sequence))
    monkeypatch.setattr(memory, 'invoke_with_retry', llm_utils.invoke_with_retry)
    monkeypatch.setattr(llm_utils, 'get_llm_max_retries', lambda: 8)
    monkeypatch.setattr(llm_utils.time, 'sleep', lambda _: None)
    result = memory.run_memory_review('完整证据', CFG, operation_id='budget')
    assert (result.status == 'completed') == completed and len(calls) == count
    if not completed:
        assert not result.summary and not result.projection_metadata


@pytest.mark.parametrize('corruption', ['source', 'hash', 'reference', 'permission', 'ban', 'omitted', 'length', 'tool', 'refusal'])
def test_semantic_incomplete_tool_and_refusal_never_correct(sources, monkeypatch, corruption):
    context = projections.prepare_review('cfg')
    value = response_for(context)
    if corruption in {'source', 'hash'}:
        value['projections'][0]['source_' + ('id' if corruption == 'source' else 'hash')] = 'forged'
    elif corruption == 'reference':
        value['memory']['execution_refs'] = ['forged']
    elif corruption == 'permission':
        value['memory']['allowed'] = True
    elif corruption == 'ban':
        value['memory']['strategy_text'] = '旧 unknown 未核验所以禁止加仓'
    elif corruption == 'omitted':
        value['projections'] = []
    item = response(value, **({'finish_reason': 'length'} if corruption == 'length' else {}))
    if corruption == 'tool':
        item.tool_calls = [{'name': 'place_order', 'args': {}, 'id': 'forbidden'}]
    if corruption == 'refusal':
        item.additional_kwargs = {'refusal': 'refused'}
    calls, _, _ = scripted_model(monkeypatch, item)
    result = memory.run_memory_review('完整证据', CFG, operation_id='reject')
    assert result.status == 'failed' and not result.projection_metadata and len(calls) == 1


def test_real_audit_preserves_failed_response_and_successful_correction(sources, monkeypatch):
    from backend import database_agent_runs as audit
    context = projections.prepare_review('cfg')
    calls, costs, _ = scripted_model(monkeypatch, response({'summary': LEGACY}), response(response_for(context)))
    monkeypatch.setattr(memory, '_start_audit', START_AUDIT)
    monkeypatch.setattr(memory, '_finish_audit', FINISH_AUDIT)
    assert memory.run_memory_review('来源', CFG, operation_id='audit-correction').status == 'completed'
    rows = audit.list_agent_runs(config_id='cfg', purpose='memory_review')['runs']
    runs = sorted((audit.get_agent_run(row['run_id']) for row in rows), key=lambda r: r['details']['attempt'])
    assert len(runs) == len(calls) == len(costs) == 2
    assert [row['status'] for row in runs] == ['error', 'success']
    assert runs[0]['output'] == response({'summary': LEGACY}).content
    assert all(row['details']['operation_id'] == 'audit-correction' for row in runs)
