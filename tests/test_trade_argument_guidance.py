"""Regression for malformed model calls and the schemas actually bound to models."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import TypeAdapter, ValidationError

from backend.agent import agent_graph, tool_registry
from backend.agent.agent_models import AgentState
from backend.agent.trade_batch import TradeActionsSchema
from backend.agent.trading_contracts import TradeAction, UnifiedTradeActionsSchema
from backend.agent.unified_trading import unified_schema_for_config, validate_for_config
from backend.config import config


CFG = {'config_id': 'cfg', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}
VALID_EDIT = {'action': 'edit', 'order_id': 'algo:current-id', 'trigger_price': 2351.8, 'reason': 'adjust stop'}
INVALID = [
    ({'action_type': 'edit', 'trigger_price': 2351.8, 'reason': 'adjust stop'}, 'action, not action_type'),
    ({'action': 'edit', 'execution_order_id': 'not-a-current-order-id', 'trigger_price': 2351.8,
      'reason': 'adjust stop'}, 'order_id is required'),
    ({'action': 'cancel', 'operation_id': 'request-not-order', 'reason': 'cancel'}, 'order_id is required'),
]


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('action,hint', INVALID)
def test_invalid_batch_has_action_index_and_correction_hint_without_rewriting(version, action, hint):
    request = {'contract_version': version, 'actions': [action]}
    original = copy.deepcopy(request)
    with pytest.raises(ValidationError, match=hint) as error:
        TradeActionsSchema.model_validate(request)
    assert error.value.errors()[0]['loc'] == ('actions', 0)
    assert request == original
    # MCP v2 validates its union directly before envelope validation.
    with pytest.raises(ValidationError, match=hint):
        TypeAdapter(list[TradeAction]).validate_python([action])


@pytest.mark.parametrize('action', [None, [], {}, True, 'unsupported'])
def test_invalid_discriminator_type_still_returns_validation_error(action):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate({'contract_version': 3, 'actions': [{'action': action}]})


@pytest.mark.parametrize('action,hint', INVALID)
def test_actual_agent_tool_message_explains_rejection_without_dispatch(monkeypatch, action, hint):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    execute = Mock(side_effect=AssertionError('invalid action reached execution'))
    monkeypatch.setattr('backend.agent.unified_trading.execute_v3_actions', execute)
    monkeypatch.setattr(agent_graph, '_emit_task_progress', lambda *args, **kwargs: None)
    state = AgentState(symbol='ETH/USDT', market_context={}, account_context={}, messages=[
        AIMessage(content='', tool_calls=[{'id': 'bad-parameters', 'name': 'execute_trade_actions',
                                          'args': {'contract_version': 3, 'actions': [action]}}])])
    result = agent_graph.tools_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': CFG}})
    assert hint in result.messages[-1].content
    execute.assert_not_called()


@pytest.mark.parametrize('mode,exit_mode', [
    ('REAL', 'independent_exits'), ('STRATEGY', 'independent_exits'),
    ('REAL', 'attached_required'), ('STRATEGY', 'attached_optional'), ('SPOT_DCA', 'independent_exits'),
])
def test_bound_model_schema_keeps_required_identity_and_mode_restrictions(mode, exit_mode):
    cfg = {**CFG, 'mode': mode, 'exit_mode': exit_mode}
    bound = convert_to_openai_tool(tool_registry.get_trade_tools_for_mode(mode, cfg)[0])['function']
    assert 'action (not action_type)' in bound['description']
    assert 'operation_id is a request identity' in bound['description']
    items = bound['parameters']['properties']['actions']['items']
    edit = next(item for item in items['oneOf'] if item['properties']['action']['const'] == 'edit')
    assert {'action', 'reason', 'order_id'} <= set(edit['required'])
    assert 'query open orders' in edit['properties']['order_id']['description']
    assert 'action_type' not in edit['properties'] and edit['additionalProperties'] is False
    assert ('trigger_price' in edit['properties']) == (mode != 'SPOT_DCA' and exit_mode == 'independent_exits')


def test_corrected_parameters_keep_identity_serialization_and_real_dispatch(monkeypatch):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    execute = Mock(return_value={'status': 'completed', 'results': []})
    monkeypatch.setattr('backend.agent.unified_trading.execute_v3_actions', execute)
    payload = {'contract_version': 3, 'actions': [dict(VALID_EDIT)]}
    original = copy.deepcopy(payload)
    assert UnifiedTradeActionsSchema.model_validate(payload).model_dump(exclude_none=True) == payload
    parsed = validate_for_config(payload, CFG)
    assert parsed.actions[0].order_id == 'algo:current-id'
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'corrected-parameters'))
    assert result['status'] == 'completed'
    execute.assert_called_once()
    assert execute.call_args.args[0]['actions'] == payload['actions']
    assert payload == original


def test_paper_schema_keeps_current_order_requirement():
    schema = unified_schema_for_config({'mode': 'PAPER'}, engine='paper')
    edit = schema['$defs']['UnifiedEditAction']
    assert 'order_id' in edit['required']
    assert 'operation_id' in edit['properties']['order_id']['description']
    assert validate_for_config({'contract_version': 3, 'actions': [VALID_EDIT]},
                               {'mode': 'PAPER'}, engine='paper').actions[0].order_id == VALID_EDIT['order_id']
