"""Offline SDK traces include rejected output, without model or LangSmith requests."""
import json

import pytest
from langchain_core.messages import AIMessage
from langsmith import get_current_run_tree, tracing_context

from backend import database, database_agent_runs
from backend.agent import call_audit, memory_agent, memory_service
from backend.database_schema import initialize_schema
from backend.utils.llm_utils import require_complete_response


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'trace.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(memory_agent.global_config, 'langchain_background_tracing', False)
    monkeypatch.setattr(memory_agent, 'invoke_with_retry', lambda invoke, **_: invoke())


@pytest.mark.parametrize('purpose', ['strategy_summary', 'memory_review'])
@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_trace_validation_retains_full_response_and_local_correlation(isolated, monkeypatch, purpose, finish):
    spans = []
    response = AIMessage(content=json.dumps({'summary': '完整记忆；' * 500 + '尾部风险条件'}),
                         response_metadata={'finish_reason': finish, 'authorization': 'do-not-record'},
                         additional_kwargs={'reasoning_content': '完整推理'},
                         usage_metadata={'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30,
                                         'output_token_details': {'reasoning': 15}})
    settings = {'extra_body': {'max_tokens': 4096}, 'thinking_enabled': True, 'reasoning_effort': 'high'}
    def invoke(*args):
        spans.append(get_current_run_tree())
        return response
    with tracing_context(enabled='local'):
        if purpose == 'strategy_summary':
            def run():
                return call_audit.audited_invoke(invoke, config_id='cfg', purpose=purpose,
                    model='offline', messages=[], request_settings=settings,
                    response_validator=require_complete_response)
            if finish == 'length':
                with pytest.raises(ValueError, match='finish_reason=length'):
                    run()
            else:
                assert run() is response
        else:
            from types import SimpleNamespace
            monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: SimpleNamespace(invoke=invoke))
            result = memory_agent.run_memory_review('旧记忆与完整新证据',
                {'config_id': 'cfg', 'summarizer': {'model': 'offline', **settings}}, operation_id='op-1')
            assert result.status == ('failed' if finish == 'length' else 'completed')
            if finish == 'stop':
                assert result.summary == json.loads(response.content)['summary']
    assert len(spans) == 1
    span = spans[0]
    assert span.name == purpose
    assert bool(span.error) == (finish == 'length')
    assert span.outputs['output'] == response.content
    assert span.outputs['details']['response_metadata']['finish_reason'] == finish
    assert span.outputs['details']['reasoning_content'] == '完整推理'
    assert 'do-not-record' not in str(span.outputs)
    assert span.metadata['request_settings']['max_tokens'] == 4096
    record = database_agent_runs.get_agent_run(span.id.hex)
    assert record['status'] == ('error' if finish == 'length' else 'success')
    assert record['output'] == response.content
    assert record['details']['request_settings']['thinking_enabled'] is True
    assert record['details']['response_metadata']['finish_reason'] == finish
    assert record['completion_tokens'] == 20
    assert record['details']['usage']['output_token_details']['reasoning'] == 15


@pytest.mark.parametrize('purpose', ['strategy_summary', 'memory_review'])
def test_main_tracing_disable_is_respected(isolated, monkeypatch, purpose):
    from langsmith import utils
    calls = []
    def invoke(*args):
        calls.append(1)
        assert not utils.tracing_is_enabled()
        return AIMessage(content='{"summary":"保留必要条件"}')
    with tracing_context(enabled=False):
        if purpose == 'strategy_summary':
            call_audit.audited_invoke(invoke, config_id='cfg', purpose=purpose, model='offline', messages=[])
        else:
            from types import SimpleNamespace
            monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: SimpleNamespace(invoke=invoke))
            result = memory_agent.run_memory_review('evidence', {'config_id': 'cfg', 'summarizer': {'model': 'offline'}}, operation_id='op-off')
            assert result.status == 'completed'
    assert calls == [1]


def test_memory_service_propagates_provider_reason_without_overwriting_memory(monkeypatch):
    monkeypatch.setattr(memory_service, 'get_review_result', lambda _: None)
    monkeypatch.setattr(memory_agent, 'run_memory_review', lambda *_, **__: memory_agent.MemoryReviewResult(
        error='Memory review response is incomplete: finish_reason=length'))
    def unexpected_write(*args, **kwargs):
        pytest.fail('Failed memory must not be saved')
    monkeypatch.setattr(memory_service, 'save_review_result', unexpected_write)
    monkeypatch.setattr(memory_service, 'save_short_memory', unexpected_write)
    with pytest.raises(RuntimeError, match='finish_reason=length'):
        memory_service.organize_memory('evidence', {'config_id': 'cfg'}, operation_id='op-1')


def test_trace_failure_does_not_replay_model_or_hide_validation(isolated, monkeypatch):
    import langsmith
    calls = []
    class BrokenTrace:
        def __enter__(self):
            raise RuntimeError('Tracing unavailable')
    monkeypatch.setattr(langsmith, 'trace', lambda *_, **__: BrokenTrace())
    def operation():
        calls.append(1)
        return AIMessage(content='完整正文', response_metadata={'finish_reason': 'length'})
    with pytest.raises(ValueError, match='length'):
        call_audit.audited_invoke(operation, config_id='cfg', purpose='strategy_summary', model='offline',
                                 messages=[], response_validator=require_complete_response)
    assert calls == [1]


def test_real_retry_wrapper_preserves_validation_reason(isolated, monkeypatch):
    from types import SimpleNamespace
    from backend.utils.llm_utils import invoke_with_retry
    calls = []
    def invoke(*args):
        calls.append(1)
        return AIMessage(content='{"summary":"不完整响应"}', response_metadata={'finish_reason': 'length'})
    monkeypatch.setattr(memory_agent, 'invoke_with_retry', invoke_with_retry)
    monkeypatch.setattr(memory_agent, 'build_chat_model', lambda **_: SimpleNamespace(invoke=invoke))
    with tracing_context(enabled=False):
        result = memory_agent.run_memory_review('evidence', {'config_id': 'cfg', 'summarizer': {'model': 'offline'}}, operation_id='real-retry')
    assert 'finish_reason=length' in result.error and result.status == 'failed'
    assert calls == [1]
