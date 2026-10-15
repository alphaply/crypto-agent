"""One model request per action; no exchange credentials or model network."""
import json

import ccxt
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from test_trade_tool_integration import simulation, real_short, call


def rejected_then_valid(exchange):
    create = exchange.create_order
    attempts = []
    def submit(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ccxt.InvalidOrder('binance {"code":-2021,"msg":"invalid trigger"}')
        return create(*args, **kwargs)
    exchange.create_order = submit
    return attempts


def stop_calls():
    return [{'id': f'stop-{i}', 'name': 'close', 'args': {
        'side': 'SHORT', 'order_type': 'stop_market', 'amount': .2,
        'trigger_price': 110 + i, 'reason': 'protect'}} for i in range(2)]


@pytest.mark.parametrize('surface', ['agent', 'chat'])
def test_same_round_rejection_does_not_block_valid_independent_action(real_short, monkeypatch, surface):
    from backend.agent import agent_graph, chat_graph
    from backend.agent.agent_models import AgentState
    _, exchange = real_short
    attempts = rejected_then_valid(exchange)
    message = AIMessage(content='', tool_calls=stop_calls())
    cfg = {'configurable': {'config_id': 'cfg', 'agent_config': {'mode': 'REAL', 'exit_mode': 'independent_exits'}}}
    approvals = []
    if surface == 'agent':
        output = agent_graph.tools_node(AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[message]), cfg).messages
    else:
        monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'mode': 'REAL', 'symbol': 'ETH/USDT', 'exit_mode': 'independent_exits'})
        monkeypatch.setattr(chat_graph, 'interrupt', lambda request: approvals.append(request) or True)
        output = chat_graph.tools_node({'messages': [message], 'symbol': 'ETH/USDT'}, cfg)['messages']
        assert len(approvals) == 2
    results = [json.loads(m.content) for m in output if isinstance(m, ToolMessage)]
    assert [r['status'] for r in results] == ['failed', 'submitted']
    assert len(attempts) == 2 and len(exchange.calls) == 2


def test_chat_user_rejection_stops_later_calls_including_cached_retired_tools(real_short, monkeypatch):
    from backend.agent import chat_graph
    _, exchange = real_short
    calls = stop_calls() + [{'id': 'read', 'name': 'get_trade_operation', 'args': {'target_operation_id': 'absent'}}]
    monkeypatch.setattr(chat_graph, '_resolve_chat_config', lambda _: {'mode': 'REAL', 'symbol': 'ETH/USDT', 'exit_mode': 'independent_exits'})
    approvals = []
    monkeypatch.setattr(chat_graph, 'interrupt', lambda request: approvals.append(request) or False)
    output = chat_graph.tools_node({'messages': [AIMessage(content='', tool_calls=calls)]}, {'configurable': {'config_id': 'cfg'}})['messages']
    assert output[0].content == 'Rejected by user.'
    assert json.loads(output[1].content)['status'] == 'not_executed'
    assert json.loads(output[2].content)['status'] == 'not_executed'
    assert len(approvals) == 1 and len(exchange.calls) == 1


def test_agent_cached_removed_query_is_rejected_without_running_the_next_write(real_short):
    from backend.agent.agent_graph import tools_node, AgentState
    _, exchange = real_short
    attempts = rejected_then_valid(exchange)
    calls = stop_calls()
    calls.insert(1, {'id': 'query-rejected', 'name': 'get_trade_operation', 'args': {'target_operation_id': 'stop-0'}})
    state = AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[AIMessage(content='', tool_calls=calls)])
    output = tools_node(state, {'configurable': {'config_id': 'cfg'}})
    results = [json.loads(m.content) for m in output.messages if isinstance(m, ToolMessage)]
    assert [r['status'] for r in results] == ['failed', 'failed', 'not_executed']
    assert results[1]['error_code'] == 'tool_removed'
    assert len(attempts) == 1 and len(exchange.calls) == 1


def test_independent_batch_continues_and_replay_never_sends_again(real_short):
    _, exchange = real_short
    attempts = rejected_then_valid(exchange)
    args = {'contract_version': 2, 'execution_policy': 'independent',
            'actions': [{'action': c['name'], **c['args']} for c in stop_calls()]}
    result = call('execute_trade_actions', args, 'batch')
    assert [r['status'] for r in result['results']] == ['failed', 'submitted']
    assert result['summary']['submitted'] == 1
    call('execute_trade_actions', args, 'batch')
    assert len(attempts) == 2


def test_independent_batch_unknown_only_blocks_dependent_writes(real_short):
    _, exchange = real_short
    stop = call('close', dict(side='SHORT', order_type='stop_market', amount=.2, trigger_price=110, reason='protect'), 'owned-stop')
    original_create, original_query = exchange.create_order, exchange.fetch_order
    unknown = []
    def send(*args, **kwargs):
        exchange.timeout_next = True
        try:
            return original_create(*args, **kwargs)
        finally:
            unknown.append(str(len(exchange.orders)))
    def query(oid, *args, **kwargs):
        if str(oid) in unknown or oid is None:
            raise ccxt.OrderNotFound('unknown execution')
        return original_query(oid, *args, **kwargs)
    exchange.create_order, exchange.fetch_order = send, query
    args = {'contract_version': 2, 'execution_policy': 'independent', 'actions': [
        {'action': 'close', 'side': 'SHORT', 'order_type': 'market', 'amount': .2, 'reason': 'reduce'},
        {'action': 'close', 'side': 'SHORT', 'order_type': 'market', 'amount': .2, 'reason': 'depends on current position'},
        {'action': 'cancel', 'order_id': stop['order_id'], 'reason': 'independent target'}]}
    result = call('execute_trade_actions', args, 'unknown-batch')
    assert [r['status'] for r in result['results']] == ['unknown', 'not_executed', 'completed']
    assert result['status'] == 'unknown' and len(exchange.calls) == 3
    call('execute_trade_actions', args, 'unknown-batch')
    assert len(exchange.calls) == 3


@pytest.mark.parametrize('dependency_status', ['failed', 'unknown', 'submitted', 'skipped'])
def test_only_completed_dependency_allows_successor(simulation, monkeypatch, dependency_status):
    from backend.agent import independent_tools
    requests = []
    def execute(action, args, *unused):
        requests.append(args)
        assert 'depends_on' not in args
        return json.dumps({'receipt_version': 1, 'status': dependency_status if len(requests) == 1 else 'completed'})
    monkeypatch.setattr(independent_tools, 'execute_action', execute)
    args = {'contract_version': 2, 'execution_policy': 'independent', 'actions': [
        {'action': 'cancel', 'order_id': 'regular:1', 'reason': 'replace'},
        {'action': 'open', 'side': 'LONG', 'amount': .2, 'price': 100, 'reason': 'replacement', 'depends_on': [0]},
        {'action': 'cancel', 'order_id': 'regular:2', 'reason': 'independent'}]}
    result = call('execute_trade_actions', args, 'dependencies')
    assert result['results'][1]['status'] == 'not_executed'
    assert result['results'][1]['blocked_by_indices'] == [0]
    assert result['results'][2]['status'] == 'completed'
    assert len(requests) == 2
    call('execute_trade_actions', args, 'dependencies')
    assert len(requests) == 2
