import json
from unittest.mock import Mock

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
import pytest

from backend import database
from backend.agent import agent_graph, decision_context
from backend.agent.agent_models import AgentState
from backend.agent.tool_registry import get_trade_tools_for_mode
from backend.app.core import scheduler
from backend.database_schema import initialize_schema


@pytest.mark.parametrize('trade_mode', ['REAL', 'STRATEGY'])
@pytest.mark.parametrize('exit_mode', ['attached_required', 'attached_optional', 'independent_exits'])
def test_bound_contract_matches_exit_mode_without_shared_mutation(trade_mode, exit_mode):
    tools = {tool.name: convert_to_openai_tool(tool)['function'] for tool in
             get_trade_tools_for_mode(trade_mode, {'exit_mode': exit_mode})}
    opening = tools[f'open_position_{trade_mode.lower()}']['parameters']['properties']['orders']['items']
    batch_opening = tools['execute_trade_actions']['parameters']['properties']['actions']['items']['oneOf'][0]['properties']['order']
    for schema in (opening, batch_opening):
        for field in ('stop_loss', 'take_profit'):
            assert (field in schema['properties']) == (exit_mode != 'independent_exits')
            assert (field in schema['required']) == (exit_mode == 'attached_required')
    independent = exit_mode == 'independent_exits'
    assert ('update_exit_order' in tools) == independent
    assert (f'update_position_protection_{trade_mode.lower()}' in tools) != independent
    if independent:
        assert '待核验时不能' in tools[f'close_position_{trade_mode.lower()}']['description']
        amendments = tools[f'update_entry_order_{trade_mode.lower()}']['parameters']['properties']
        assert 'stop_loss' not in amendments
    # The shared runtime validators must remain available to reject forged calls.
    original = next(t for t in get_trade_tools_for_mode(trade_mode) if t.name.startswith('open_position_'))
    assert 'stop_loss' in convert_to_openai_tool(original)['function']['parameters']['properties']['orders']['items']['properties']


def test_retired_rules_hourly_and_mode_sections_removed_from_custom_template():
    template = ('## 长期交易规则\nold rules\n## 当前小时级策略周期\nold hourly\n'
                '## Account\n{positions_text}\n当前退出模式：exit_mode=independent_exits。\n'
                '## Renamed rules\n{trading_rules_text}\n## Custom\nkeep')
    result = decision_context.render_decision_prompt(template, decision_context.DecisionMemory('memory', 'recent', 'rules'), positions_text='position')
    assert all(value not in result for value in ('old rules', 'old hourly', 'exit_mode=', 'Renamed rules', '长期交易规则'))
    assert all(value in result for value in ('position', 'keep', 'memory', 'recent'))


def test_stream_with_no_reasoning_still_publishes_model_and_tool_progress():
    model = Mock()
    model.stream.return_value = iter([AIMessageChunk(content='Checking orders', tool_call_chunks=[
        {'id': 'call', 'name': 'cancel_orders_real', 'args': '{"order_id":"one","reason":"expired"}', 'index': 0},
    ])])
    events = []
    agent_graph._stream_agent_response(model, [HumanMessage(content='act')], configurable={'progress_callback': events.append})
    entry = events[-1]['decision']['messages'][0]
    assert entry['content'] == 'Checking orders'
    assert entry['tool_calls'][0]['name'] == 'cancel_orders_real'


def test_live_loop_preserves_prior_turns_receipts_and_skipped_calls_after_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'progress.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    scheduled = '2026-10-10 12:00:00'
    scheduler._insert_scheduler_run('cfg', scheduler.AGENT_JOB_TYPE, scheduled)
    events = []

    def progress(event):
        events.append(event)
        scheduler._mark_scheduler_progress('cfg', scheduled, event)

    cfg = {'configurable': {'config_id': 'cfg', 'progress_callback': progress}}
    run = Mock(side_effect=['{"status":"completed","order_id":"one"}', '{"status":"unknown"}'])
    monkeypatch.setattr(agent_graph, 'run_trade_tool', run)
    state = AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[
        HumanMessage(content='private system prompt'),
        AIMessage(content='first', tool_calls=[{'id': 'one', 'name': 'cancel_orders_real', 'args': {'order_id': 'one'}}]),
    ])
    state = agent_graph.tools_node(state, cfg)
    state.messages.append(AIMessage(content='second', tool_calls=[
        {'id': 'two', 'name': 'open_position_real', 'args': {}},
        {'id': 'three', 'name': 'open_position_real', 'args': {}},
    ]))
    state = agent_graph.tools_node(state, cfg)
    assert run.call_count == 2
    with database.get_db_conn() as conn:
        saved = json.loads(conn.execute('SELECT decision_json FROM scheduler_runs').fetchone()[0])
    assert [entry['role'] for entry in saved['messages']] == ['assistant', 'tool', 'assistant', 'tool', 'tool']
    assert [item['status'] for item in saved['execution_results']] == ['completed', 'unknown', 'skipped']
    assert 'private system prompt' not in json.dumps(saved)
    assert any(event['phase'] == 'tool_running' and event['decision']['execution_results'] for event in events)
