"""Exercise real finalize-to-summary wiring with isolated storage and mocked models."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend import database, database_agent_runs
from backend.agent import agent_graph
from backend.agent.agent_models import AgentState
from backend.database_schema import initialize_schema
from backend.utils.decision_journal import decision_journal
from backend.utils.trading_policy import SUMMARY_FACT_POLICY


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'strategy-summary.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    for field in ('model', 'api_key', 'api_base'):
        monkeypatch.setattr(agent_graph.global_config, f'global_summarizer_{field}', '')
        monkeypatch.delenv(f'GLOBAL_SUMMARIZER_{field.upper()}', raising=False)
    monkeypatch.setattr(agent_graph, 'invoke_with_retry', lambda operation, **_: operation())


def state_with(messages):
    return AgentState(symbol='ETH/USDT', messages=messages, market_context={}, account_context={},
                      history_context=[], active_model_name='trade-model')


def saved_summary():
    with database.get_db_conn() as conn:
        rows = conn.execute('SELECT * FROM summaries WHERE config_id = ?', ('cfg',)).fetchall()
    assert len(rows) == 1
    return dict(rows[0])


@pytest.mark.parametrize('mode', ['STRATEGY', 'REAL', 'SPOT_DCA'])
@pytest.mark.parametrize('role', ['system', 'user'])
def test_finalize_uses_configured_deepseek_and_saves_its_complete_summary(local_db, monkeypatch, mode, role):
    analysis = 'Complete market analysis. ' * 300 + 'ANALYSIS_TAIL'
    receipt = '{"status":"completed","details":"' + 'Receipt detail. ' * 300 + 'RECEIPT_TAIL"}'
    result = 'Model-compressed strategy with all required conditions. ' * 65 + 'SUMMARY_TAIL'
    messages = [
        AIMessage(content=analysis, tool_calls=[{'id': 'inspect-1', 'name': 'get_account', 'args': {}}]),
        ToolMessage(content=receipt, tool_call_id='inspect-1'),
        AIMessage(content='Final decision: wait for confirmation.'),
    ]
    state = state_with(messages)
    cfg = {
        'model': 'trade-model', 'api_key': 'trade-test-key', 'api_base': 'https://trade.example.test/v1',
        'mode': mode, 'system_prompt_role': 'user' if role == 'system' else 'system',
        'strategy_prompt': 'TOP_LEVEL: 压缩为80字以内，保留结果。\n{content}',
        'summarizer': {
            'model': 'deepseek-flash', 'api_key': 'summary-test-key', 'api_base': 'https://summary.example.test/v1',
            'compatibility_mode': 'deepseek', 'system_prompt_role': role, 'temperature': 0.2,
            'thinking_enabled': False, 'reasoning_effort': 'low',
            'extra_body': {'max_tokens': 4096},
            'strategy_prompt': 'NESTED_SHOULD_NOT_WIN: {content}',
        },
    }
    original_config = deepcopy(cfg)
    report = {'version': 1, 'market_analysis': 'Market analysis', 'strategy': 'One strategy',
              'decision': {'action': 'HOLD', 'rationale': 'wait'},
              'forecast': {'next_1h': 'conditional', 'next_4h': 'conditional'},
              'risks': ['risk'], 'invalidation': 'condition', 'next_watchpoints': ['watch'], 'summary': result}
    llm = Mock(invoke=Mock(return_value=AIMessage(content=json.dumps(report), response_metadata={'finish_reason': 'stop'})))
    build = Mock(return_value=llm)
    monkeypatch.setattr(agent_graph, 'build_chat_model', build)
    trade = Mock(side_effect=AssertionError('Finalize must never replay trading tools'))
    monkeypatch.setattr(agent_graph, 'run_trade_tool', trade)
    review = Mock(side_effect=AssertionError('Per-turn summaries must not trigger rule reviews'))
    monkeypatch.setattr(agent_graph, 'update_turn_memory', review)

    assert agent_graph.finalize_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': cfg}}) is state

    build.assert_called_once_with(model='deepseek-flash', api_key='summary-test-key',
                                  base_url='https://summary.example.test/v1', temperature=0.2,
                                  extra_body={'max_tokens': 4096}, thinking_enabled=False,
                                  reasoning_effort='low', compatibility_mode='deepseek')
    llm.invoke.assert_called_once()
    prompt_message = llm.invoke.call_args.args[0][0]
    assert prompt_message.type == ('human' if role == 'user' else 'system')
    assert prompt_message.content.startswith('TOP_LEVEL: 压缩为80字以内')
    assert 'NESTED_SHOULD_NOT_WIN' not in prompt_message.content
    assert decision_journal(messages) in prompt_message.content
    assert prompt_message.content.count(SUMMARY_FACT_POLICY) == 1
    saved = saved_summary()
    assert saved['strategy_logic'] == result
    structured = json.loads(saved['report_json'])
    assert structured['raw_analysis'] == analysis + '\n\n---\n\nFinal decision: wait for confirmation.'
    assert structured['execution_results'][0]['result'] == receipt
    assert '## 行情解析' in saved['content']
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM memory_update_jobs').fetchone()[0] == 1
    assert saved['agent_name'] == 'trade-model'
    assert cfg == original_config
    audits = database_agent_runs.list_agent_runs(config_id='cfg', purpose='strategy_summary')['runs']
    assert len(audits) == 1 and audits[0]['status'] == 'success' and audits[0]['model'] == 'deepseek-flash'
    trade.assert_not_called()
    review.assert_not_called()


@pytest.mark.parametrize('failure', ['exception', 'length', 'empty'])
def test_summary_failure_keeps_full_labeled_source_without_replaying_trade(local_db, monkeypatch, failure):
    messages = [AIMessage(content='Executed strategy reasoning. ' * 500 + 'RAW_TAIL'),
                ToolMessage(content='Confirmed execution receipt. ' * 100 + 'RECEIPT_TAIL', tool_call_id='order')]
    llm = Mock()
    if failure == 'exception':
        llm.invoke.side_effect = RuntimeError('Summary provider unavailable')
    else:
        llm.invoke.return_value = AIMessage(
            content='' if failure == 'empty' else 'Partial output',
            response_metadata={'finish_reason': 'stop' if failure == 'empty' else 'length'},
        )
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)
    trade = Mock(side_effect=AssertionError('Do not replay completed trading'))
    monkeypatch.setattr(agent_graph, 'run_trade_tool', trade)
    state = state_with(messages)
    progress = []

    assert agent_graph.finalize_node(state, {'configurable': {
        'config_id': 'cfg', 'agent_config': {'summarizer': {'model': 'deepseek-flash'}},
        'progress_callback': progress.append,
    }}) is state

    saved = saved_summary()
    assert saved['strategy_logic'] == agent_graph.STRATEGY_SUMMARY_FAILURE_PREFIX + decision_journal(messages)
    assert json.loads(saved['report_json'])['raw_analysis'] == messages[0].content
    assert json.loads(saved['report_json'])['validation_status'] == 'invalid'
    assert progress[-1]['phase'] == 'completed'
    audits = database_agent_runs.list_agent_runs(config_id='cfg', purpose='strategy_summary')['runs']
    assert len(audits) == 2 and all(item['status'] == 'error' for item in audits)
    assert llm.invoke.call_count == 2
    trade.assert_not_called()


@pytest.mark.parametrize('messages', [
    [], [AIMessage(content='')], [AIMessage(content=' \n\t ')],
    [AIMessage(content='Error: trade model disconnected', additional_kwargs={'invocation_failed': True})],
])
def test_failed_or_empty_trading_round_does_not_call_summary_model(local_db, monkeypatch, messages):
    build = Mock(side_effect=AssertionError('Failed trade rounds must not call summary models'))
    monkeypatch.setattr(agent_graph, 'build_chat_model', build)
    progress = []
    with pytest.raises(RuntimeError):
        agent_graph.finalize_node(state_with(messages), {'configurable': {
            'config_id': 'cfg', 'agent_config': {'model': 'trade-model'}, 'progress_callback': progress.append,
        }})
    build.assert_not_called()
    assert saved_summary()['content'].startswith('Error:')
    assert progress[-1]['phase'] == 'failed'


@pytest.mark.parametrize('summary_type', ['strategy', 'daily'])
@pytest.mark.parametrize('prompt_source', ['top_text', 'nested_text', 'top_file', 'nested_file'])
def test_summary_prompt_resolution_preserves_user_configuration(local_db, monkeypatch, tmp_path, summary_type, prompt_source):
    key = f'{summary_type}_prompt'
    cfg = {'config_id': 'cfg', 'model': 'trade-model', 'system_prompt_role': 'user',
           'summarizer': {'model': 'deepseek-flash'}}
    template = 'Configured compression target: 80 characters. Source: {content}'
    if prompt_source.endswith('text'):
        owner = cfg if prompt_source == 'top_text' else cfg['summarizer']
        owner[key] = template
        if prompt_source == 'top_text':
            cfg['summarizer'][key] = 'Unselected nested prompt'
    else:
        prompt_file = tmp_path / 'summary-prompt.txt'
        prompt_file.write_text(template, encoding='utf-8')
        owner = cfg if prompt_source == 'top_file' else cfg['summarizer']
        owner[key + '_file'] = str(prompt_file)
        if prompt_source == 'top_file':
            cfg['summarizer'][key + '_file'] = 'unselected-missing-file.txt'
    llm = Mock(invoke=Mock(return_value=AIMessage(content='Complete condensed summary')))
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)

    assert agent_graph.summarize_content('Full source', cfg, summary_type) == 'Complete condensed summary'
    message = llm.invoke.call_args.args[0][0]
    assert message.type == 'human'
    assert message.content.startswith(template.replace('{content}', 'Full source'))


def test_global_summarizer_is_used_before_trading_model(local_db, monkeypatch):
    for field, value in {'model': 'deepseek-flash', 'api_key': 'global-summary-test-key',
                         'api_base': 'https://global-summary.example.test/v1'}.items():
        monkeypatch.setattr(agent_graph.global_config, f'global_summarizer_{field}', value)
    llm = Mock(invoke=Mock(return_value=AIMessage(content='Global summary')))
    build = Mock(return_value=llm)
    monkeypatch.setattr(agent_graph, 'build_chat_model', build)
    cfg = {'config_id': 'cfg', 'model': 'trade-model', 'api_key': 'trade-test-key',
           'api_base': 'https://trade.example.test/v1'}

    assert agent_graph.summarize_content('Source', cfg) == 'Global summary'
    assert build.call_args.kwargs['model'] == 'deepseek-flash'
    assert build.call_args.kwargs['api_key'] == 'global-summary-test-key'
    assert build.call_args.kwargs['base_url'] == 'https://global-summary.example.test/v1'


@pytest.mark.parametrize('summary_type', ['strategy', 'daily'])
def test_custom_prompt_without_placeholder_still_receives_complete_source(local_db, monkeypatch, summary_type):
    source = 'Full decision and execution evidence. ' * 300 + 'FULL_SOURCE_TAIL'
    template = 'Compress the supplied evidence to 80 characters while preserving the result.'
    llm = Mock(invoke=Mock(return_value=AIMessage(content='Complete condensed result')))
    monkeypatch.setattr(agent_graph, 'build_chat_model', lambda **_: llm)
    cfg = {'config_id': 'cfg', f'{summary_type}_prompt': template, 'summarizer': {'model': 'deepseek-flash'}}

    assert agent_graph.summarize_content(source, cfg, summary_type) == 'Complete condensed result'
    prompt = llm.invoke.call_args.args[0][0].content
    assert prompt.startswith(template)
    assert source in prompt and prompt.count(source) == 1
