"""Model-response validation must report failed calls without losing diagnostics."""

from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from backend import database, database_agent_runs
from backend.agent import agent_graph
from backend.agent.call_audit import audited_invoke
from backend.database_schema import initialize_schema
from backend.utils.llm_utils import require_complete_response


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'audit-validation.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


def latest_run():
    runs = database_agent_runs.list_agent_runs(config_id='cfg')['runs']
    assert len(runs) == 1
    return database_agent_runs.get_agent_run(runs[0]['run_id'])


def invoke(response, **kwargs):
    return audited_invoke(
        lambda: response, config_id='cfg', purpose='daily_summary', model='offline',
        messages=[HumanMessage(content='Complete source evidence')], **kwargs,
    )


@pytest.mark.parametrize('usage_source', ['normalized', 'legacy'])
def test_validator_failure_audits_full_output_and_usage(local_db, usage_source):
    output = 'returned diagnostic text ' * 500 + 'FULL_TAIL'
    metadata = {'finish_reason': 'length'}
    kwargs = {}
    if usage_source == 'normalized':
        kwargs['usage_metadata'] = {'input_tokens': 100, 'output_tokens': 40, 'total_tokens': 140}
    else:
        metadata['token_usage'] = {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}
    response = AIMessage(content=output, response_metadata=metadata, **kwargs)

    with pytest.raises(ValueError, match='finish_reason=length'):
        invoke(response, response_validator=require_complete_response)

    saved = latest_run()
    assert saved['status'] == 'error'
    assert saved['output'] == output and saved['output_chars'] == len(output)
    assert saved['prompt_tokens'] == 100 and saved['completion_tokens'] == 40 and saved['total_tokens'] == 140
    assert 'finish_reason=length' in saved['error']


def test_no_validator_preserves_existing_caller_behavior(local_db):
    response = AIMessage(content='Caller accepts this output', response_metadata={'finish_reason': 'length'})
    assert invoke(response) is response
    assert latest_run()['status'] == 'success'


def test_completed_response_is_validated_once_and_audited_successfully(local_db):
    response = AIMessage(content='Complete answer')
    validator = Mock(side_effect=require_complete_response)
    assert invoke(response, response_validator=validator) is response
    validator.assert_called_once_with(response)
    assert latest_run()['status'] == 'success'


@pytest.mark.parametrize('failure_boundary', ['start', 'finish'])
def test_audit_storage_error_does_not_mask_validation_failure(local_db, monkeypatch, failure_boundary):
    monkeypatch.setattr(database_agent_runs, f'{failure_boundary}_agent_run',
                        Mock(side_effect=RuntimeError('Audit unavailable')))
    response = AIMessage(content='Partial output', response_metadata={'finish_reason': 'max_tokens'})
    with pytest.raises(ValueError, match='finish_reason=max_tokens'):
        invoke(response, response_validator=require_complete_response)


@pytest.mark.parametrize('summary_type', ['strategy', 'daily'])
@pytest.mark.parametrize('failure', ['length', 'empty'])
def test_summary_rejection_is_an_error_audit_with_billable_usage(local_db, monkeypatch, summary_type, failure):
    source = 'Full strategy source ' * 400 + 'SOURCE_TAIL'
    output = '' if failure == 'empty' else 'Partial response ' * 400 + 'RESPONSE_TAIL'
    response = AIMessage(
        content=output,
        response_metadata={'finish_reason': 'stop' if failure == 'empty' else 'length'},
        usage_metadata={'input_tokens': 75, 'output_tokens': 25, 'total_tokens': 100},
    )
    model = Mock(invoke=Mock(return_value=response))
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: model)
    monkeypatch.setattr(agent_graph, 'invoke_with_retry', lambda operation, **_: operation())
    save_usage = Mock()
    monkeypatch.setattr(database, 'save_token_usage', save_usage)
    config = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'summarizer': {'model': 'offline'}}

    result = agent_graph.summarize_content(source, config, summary_type)

    assert result == (agent_graph.STRATEGY_SUMMARY_FAILURE_PREFIX + source if summary_type == 'strategy' else '')
    saved = latest_run()
    assert saved['status'] == 'error' and saved['error']
    assert saved['output'] == output
    assert saved['prompt_tokens'] == 75 and saved['completion_tokens'] == 25 and saved['total_tokens'] == 100
    assert source in saved['messages'][0]['content']
    save_usage.assert_called_once_with(symbol='ETH/USDT', config_id='cfg', model='offline',
                                       prompt_tokens=75, completion_tokens=25)
