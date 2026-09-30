from datetime import datetime
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend import database
from backend.agent import agent_graph, chat_graph
from backend.agent.agent_models import AgentState
from backend.database_memory import SummaryMemoryStore
from backend.database_schema import initialize_schema


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'context.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)


def test_daily_memory_is_seven_calendar_days_not_seven_rows(local_db):
    store = SummaryMemoryStore(database.get_db_conn, lambda: datetime(2026, 10, 1, 12), Mock())
    for day in ('2026-09-01', '2026-09-24', '2026-09-25', '2026-09-30', '2026-10-01', '2026-10-02'):
        store.save_daily_summary(day, 'ETH/USDT', 'cfg', day, 1)
    assert [x['date'] for x in store.get_daily_summaries('cfg')] == ['2026-10-01', '2026-09-30', '2026-09-25']


def test_recent_decisions_are_three_chronological_bounded_summaries(local_db):
    with database.get_db_conn() as conn:
        for index in range(4):
            conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic) VALUES(?,?,?)',
                         ('cfg', f'2026-10-01 0{index}:00:00', str(index) * 700))
        conn.execute('INSERT INTO summaries(config_id,timestamp,strategy_logic) VALUES(?,?,?)', ('other', '2026-10-01', 'other task'))
        conn.commit()
    lines = agent_graph.format_recent_decisions('cfg').splitlines()
    assert len(lines) == 3
    assert all(len(line.split('] ', 1)[1]) == 500 for line in lines)
    assert '01:00:00' in lines[0] and '03:00:00' in lines[2]
    assert 'other task' not in '\n'.join(lines)


def test_market_failure_still_includes_rules_recent_and_working_memory(local_db, monkeypatch):
    from backend.database_rules import change_trading_rules
    change_trading_rules('cfg', [{'action': 'add', 'content': 'Verify executed amounts'}], actor='human')
    database.save_short_memory('2026-10-01 01:00:00', '2026-10-01 02:00:00', 'ETH/USDT', 'cfg', 'Previous thesis', '', 1)
    market = Mock()
    market.get_market_analysis.side_effect = RuntimeError('Exchange offline')
    monkeypatch.setattr(agent_graph, 'MarketTool', lambda **_: market)
    monkeypatch.setattr(agent_graph, 'resolve_prompt_template', lambda *_: 'Custom prompt {symbol}')
    monkeypatch.setattr(agent_graph.global_config, 'get_leverage', lambda *_: 2)
    state = AgentState(symbol='ETH/USDT', messages=[], market_context={}, account_context={}, history_context=[])
    result = agent_graph.start_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': {'mode': 'STRATEGY'}}})
    prompt = result.messages[0].content
    assert 'Verify executed amounts' in prompt and 'Previous thesis' in prompt
    assert '[覆盖 2026-10-01 01:00:00 → 2026-10-01 02:00:00' in prompt
    assert '最近三轮决策摘要' in prompt and 'exit_mode=attached_required' in prompt


def test_compaction_never_splits_tool_request_from_result(monkeypatch):
    monkeypatch.setattr(chat_graph, 'CHAT_CONTEXT_RECENT_MESSAGES', 3)
    monkeypatch.setattr(chat_graph, 'CHAT_CONTEXT_COMPACTION_BATCH', 1)
    history = [HumanMessage(content='Adjust position'),
               AIMessage(content='Reduce the position', tool_calls=[{'name': 'close_position_real', 'args': {'amount': .4}, 'id': 'close'}]),
               ToolMessage(content='rejected', tool_call_id='close'), AIMessage(content='Close was rejected'),
               HumanMessage(content='Next question'), AIMessage(content='Next answer')]
    assert chat_graph._context_compaction_cutoff(history, 0, force=True) == 0
    monkeypatch.setattr(chat_graph, 'CHAT_CONTEXT_RECENT_MESSAGES', 2)
    assert chat_graph._context_compaction_cutoff(history, 0, force=True) == 4
    captured = []
    monkeypatch.setattr(chat_graph, 'build_chat_model', lambda **_: Mock(invoke=lambda messages: captured.append(messages[0].content) or AIMessage(content='Exit was rejected')))
    monkeypatch.setattr(chat_graph, 'invoke_with_retry', lambda operation, **_: operation())
    summary, cursor = chat_graph._compact_chat_context(history, '', 0, {'model': 'test'}, {}, force=True)
    assert cursor == 4 and summary == 'Exit was rejected'
    assert 'close_position_real' in captured[0] and 'Tool: rejected' in captured[0]
    assert '"amount": 0.4' in captured[0] and 'Reduce the position' in captured[0]


def test_rule_chat_tool_needs_no_approval_and_read_only_never_executes(monkeypatch):
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'symbol': 'ETH/USDT'})
    approve = Mock(side_effect=AssertionError('Rule tool should apply directly'))
    run = Mock(return_value='saved')
    monkeypatch.setattr(chat_graph, 'interrupt', approve)
    monkeypatch.setattr(chat_graph, '_run_tool', run)
    state = {'messages': [AIMessage(content='', tool_calls=[{'name': 'manage_trading_rules', 'args': {'action': 'list'}, 'id': 'rule-call'}])]}
    chat_graph.tools_node(state, {'configurable': {'config_id': 'cfg'}})
    run.assert_called_once_with('manage_trading_rules', {'action': 'list'}, 'cfg', 'ETH/USDT', operation_id='rule-call')
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'symbol': 'ETH/USDT', 'read_only': True})
    result = chat_graph.tools_node(state, {'configurable': {}})
    assert 'Read-only' in result['messages'][0].content
    assert run.call_count == 1


def test_independent_exit_context_exposes_order_ids_types_quantities_and_uncertainty():
    from backend.utils.exit_context import independent_exit_context
    account = {'independent_exits': {'exits': [
        {'order_id': 'stop-order', 'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90,
         'remaining': .4, 'status': 'OPEN', 'pending': False}
    ], 'uncovered': {'LONG': .6, 'SHORT': 0}, 'pending': False}}
    rendered = independent_exit_context(account)
    assert 'stop-order' in rendered and 'stop_market' in rendered and 'trigger=90' in rendered
    assert 'remaining=0.4' in rendered and 'LONG=0.6' in rendered
    assert '附带 TP/SL 为空不代表没有独立退出单' in rendered
    real = {'real_positions': [{'side': 'LONG', 'amount': 1}]}
    plans = [{'symbol': 'ETH/USDT:USDT', 'state': 'ACTIVE', 'side': 'LONG', 'execution_mode': 'independent_exits',
              'exits': [{'id': 'real-stop', 'exit_type': 'stop_market', 'trigger_price': 90,
                         'status': 'open', 'amount': 40, 'filled': 0}]}]
    rendered = independent_exit_context(real, plans=plans, contract_size=.01, symbol='ETH/USDT')
    assert 'remaining=0.4' in rendered and 'LONG=0.6' in rendered and '本地核验记录' in rendered
    plans[0]['exits'][0].update(id=None, status='submitting')
    rendered = independent_exit_context(real, plans=plans, contract_size=.01, symbol='ETH/USDT')
    assert '待核验=True' in rendered and 'LONG=1.0' in rendered
    assert 'LONG=未知' in independent_exit_context(real, plans=plans, contract_size=None, symbol='ETH/USDT')


@pytest.mark.parametrize('result', ['❌ Rejected', '{"status":"unknown"}'])
def test_scheduler_and_chat_stop_multi_tool_calls_after_failed_or_uncertain_result(monkeypatch, result):
    calls = [{'name': 'open_position_real', 'args': {}, 'id': f'call-{index}'} for index in range(3)]
    message = AIMessage(content='', tool_calls=calls)
    run = Mock(return_value=result)
    monkeypatch.setattr(agent_graph, 'run_trade_tool', run)
    state = AgentState(symbol='ETH/USDT', messages=[message], market_context={}, account_context={}, history_context=[])
    outputs = agent_graph.tools_node(state, {'configurable': {'config_id': 'cfg'}}).messages[1:]
    assert [output.tool_call_id for output in outputs] == ['call-0', 'call-1', 'call-2']
    assert all('not_executed' in output.content for output in outputs[1:])
    assert run.call_count == 1
    approve = Mock(return_value=True)
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'symbol': 'ETH/USDT'})
    monkeypatch.setattr(chat_graph, '_run_tool', run)
    monkeypatch.setattr(chat_graph, 'interrupt', approve)
    chat_outputs = chat_graph.tools_node({'messages': [message]}, {'configurable': {'config_id': 'cfg'}})['messages']
    assert len(chat_outputs) == 3 and all('not_executed' in output.content for output in chat_outputs[1:])
    assert run.call_count == 2 and approve.call_count == 1
