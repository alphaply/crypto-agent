"""Model-visible dependencies and runtime rejection have the same contract."""
import json
import asyncio
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from mcp.server.fastmcp.exceptions import ToolError

from backend.agent import tool_registry, independent_tools
from backend.agent.trade_batch import TradeActionsSchema
from backend.agent.trading_contracts import CanonicalTradeActionsSchema
from test_independent_tool_contracts import independent
from test_mcp_tool_contracts import contracts, call, enable_independent_profile
from test_trade_tool_integration import simulation, call as trade_call


def actions(dependencies):
    return [{'action': 'cancel', 'order_id': 'regular:1', 'reason': 'first'},
            {'action': 'cancel', 'order_id': 'regular:2', 'reason': 'second', 'depends_on': dependencies}]


def test_model_schema_and_example_match_mcp(contracts):
    caller, *_, server = contracts
    caller['profile_ids'] = ['swap']
    enable_independent_profile()
    cfg = {'mode': 'REAL', 'exit_mode': 'independent_exits'}
    agent = next(t for t in tool_registry.get_trade_tools_for_mode('REAL', cfg, include_legacy=True) if t.name == 'execute_trade_actions')
    remote = next(t for t in asyncio.run(server.list_tools()) if t.name == 'execute_trade_actions')
    for name in ('OpenAction', 'CloseAction', 'EditAction', 'CancelAction'):
        left = agent.args_schema['$defs'][name]['properties']['depends_on']
        right = remote.inputSchema['$defs']['Unified' + name]['properties']['depends_on']
        assert left == right and left['type'] == 'array' and 'anyOf' not in left
        assert 'null' in left['description'] and 'independent' in left['description']
        assert 'depends_on' not in agent.args_schema['$defs'][name]['required']
    assert agent.args_schema['properties']['execution_policy']['description'] == remote.inputSchema['properties']['execution_policy']['description']
    example = json.loads(agent.description.split('依赖示例：')[1].split('。')[0])
    assert CanonicalTradeActionsSchema.model_validate(example).actions[1].depends_on == [0]
    assert 'independent_exits' in agent.description


@pytest.mark.parametrize('policy,deps', [(None, [0]), ('stop_on_error', [0]), (None, []),
                                         ('independent', None), ('independent', [True]),
                                         ('independent', ['0']), ('independent', [0, 0]),
                                         ('independent', [-1]), ('independent', [1]), ('independent', [2])])
def test_invalid_agent_dependencies_reject_before_operation_or_dispatch(independent, monkeypatch, policy, deps):
    guard = Mock(side_effect=AssertionError('No operation may execute'))
    monkeypatch.setattr(tool_registry, 'run_once', guard)
    monkeypatch.setattr(independent_tools, 'execute_action', guard)
    args = {'contract_version': 2, 'actions': actions(deps)}
    if policy is not None:
        args['execution_policy'] = policy
    with pytest.raises(ValidationError, match='depends_on') as caught:
        tool_registry.run_trade_tool('execute_trade_actions', args, *independent, operation_id='invalid')
    guard.assert_not_called()
    if policy != 'independent':
        assert 'actions[1].depends_on' in str(caught.value) and 'explicit execution_policy="independent"' in str(caught.value)


@pytest.mark.parametrize('policy,deps', [(None, [0]), ('stop_on_error', [0]), ('independent', None)])
def test_invalid_mcp_dependencies_do_not_dispatch(contracts, policy, deps):
    _, _, dispatch, server = contracts
    enable_independent_profile()
    args = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'operation_id': 'bad-dependency-0001',
            'contract_version': 2, 'actions': actions(deps)}
    if policy is not None:
        args['execution_policy'] = policy
    if deps is None:
        with pytest.raises(ToolError, match='depends_on'):
            call(server, 'execute_trade_actions', args)
    else:
        result = call(server, 'execute_trade_actions', args)
        assert result['status'] == 'failed'
        assert 'actions[1].depends_on' in result['error']
        assert 'explicit execution_policy="independent"' in result['error']
    dispatch.assert_not_called()


def test_omissions_and_explicit_empty_dependencies_keep_exact_request_intent():
    payload = {'contract_version': 2, 'actions': actions([])[:1]}
    assert TradeActionsSchema.model_validate(payload).model_dump() == payload
    assert CanonicalTradeActionsSchema.model_validate(payload).model_dump() == payload
    payload.update(execution_policy='independent', actions=actions([]))
    assert TradeActionsSchema.model_validate(payload).model_dump() == payload
    payload['actions'][1]['depends_on'] = [0]
    assert TradeActionsSchema.model_validate(payload).model_dump() == payload


def test_completed_dependency_executes_successor_once_and_replay_keeps_identity(simulation, monkeypatch):
    dispatched = []
    def execute(action, args, *unused):
        assert 'depends_on' not in args
        dispatched.append(args['order_id'])
        return json.dumps({'receipt_version': 1, 'status': 'completed'})
    monkeypatch.setattr(independent_tools, 'execute_action', execute)
    payload = {'contract_version': 2, 'execution_policy': 'independent', 'actions': actions([0])}
    result = trade_call('execute_trade_actions', payload, 'completed-dependency')
    assert [item['status'] for item in result['results']] == ['completed', 'completed']
    assert dispatched == ['regular:1', 'regular:2']
    trade_call('execute_trade_actions', payload, 'completed-dependency')
    assert dispatched == ['regular:1', 'regular:2']
