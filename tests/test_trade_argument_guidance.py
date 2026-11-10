"""Regression for malformed model calls and the schemas actually bound to models."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import TypeAdapter, ValidationError

from backend.agent import agent_graph, chat_graph, tool_registry
from backend.agent.agent_models import AgentState
from backend.agent.trade_batch import TradeActionsSchema
from backend.agent.trading_contracts import TradeAction, UnifiedTradeActionsSchema
from backend.agent.unified_trading import unified_schema_for_config, validate_for_config
from backend.config import config
from backend.utils.decision_record import is_transient_schema_error


CFG = {'config_id': 'cfg', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits', 'symbol': 'ETH/USDT'}
VALID_EDIT = {'action': 'edit', 'order_id': 'algo:current-id', 'trigger_price': 2351.8, 'reason': 'adjust stop'}
INVALID = [
    ({'action_type': 'edit', 'trigger_price': 2351.8, 'reason': 'adjust stop'}, 'action, not action_type'),
    ({'action': 'edit', 'execution_order_id': 'not-a-current-order-id', 'trigger_price': 2351.8,
      'reason': 'adjust stop'}, 'order_id is required'),
    ({'action': 'cancel', 'operation_id': 'request-not-order', 'reason': 'cancel'}, 'order_id is required'),
    ({'action': 'close', 'order_type': 'market', 'close_all': True, 'reason': 'exit'}, 'close.side is required'),
    ({'action': 'close', 'order_type': 'STOP_MARKET', 'amount': .12,
      'trigger_price': 2349.2, 'reason': 'stop'}, 'close.side is required'),
]


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('action,hint', INVALID)
def test_invalid_batch_has_action_index_and_correction_hint_without_rewriting(version, action, hint):
    request = {'contract_version': version, 'actions': [action]}
    original = copy.deepcopy(request)
    with pytest.raises(ValidationError) as error:
        TradeActionsSchema.model_validate(request)
    detail = error.value.errors()[0]
    if version == 3 and action.get('action') == 'close':
        assert detail['loc'] == ('actions', 0, 'close', 'side') and detail['type'] == 'missing'
    else:
        assert detail['loc'] == ('actions', 0) and hint in detail['msg']
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
                                          'args': {'contract_version': 3, 'actions': [dict(VALID_EDIT), action]}}])])
    result = agent_graph.tools_node(state, {'configurable': {'config_id': 'cfg', 'agent_config': CFG}})
    reason = '缺少 side。平多填 LONG，平空填 SHORT。' if action.get('action') == 'close' else hint
    message = result.messages[-1].content
    assert reason in message and '第 2 项' in message and '本批未提交' in message
    assert is_transient_schema_error(message)
    execute.assert_not_called()


@pytest.mark.parametrize('mode,exit_mode', [
    ('REAL', 'independent_exits'), ('STRATEGY', 'independent_exits'),
    ('REAL', 'attached_required'), ('STRATEGY', 'attached_optional'), ('SPOT_DCA', 'independent_exits'),
])
def test_bound_model_schema_keeps_required_identity_and_mode_restrictions(mode, exit_mode):
    cfg = {**CFG, 'mode': mode, 'exit_mode': exit_mode}
    bound = {tool.name: convert_to_openai_tool(tool)['function']
             for tool in tool_registry.get_trade_tools_for_mode(mode, cfg)}
    edit = bound['edit_order']['parameters']
    assert {'reason', 'order_id'} <= set(edit['required'])
    assert 'query open orders' in edit['properties']['order_id']['description']
    assert 'action_type' not in edit['properties'] and edit['additionalProperties'] is False
    assert ('trigger_price' in edit['properties']) == (mode != 'SPOT_DCA' and exit_mode == 'independent_exits')
    close = bound['close_position']['parameters']
    assert {'reason', 'side', 'order_type'} <= set(close['required'])
    assert ('SHORT closes a short' in close['properties']['side']['description']) == (mode != 'SPOT_DCA')
    if mode == 'SPOT_DCA':
        assert 'SHORT' not in json.dumps(bound) and 'close_all' not in json.dumps(bound)
    assert 'lowercase' in close['properties']['order_type']['description']
    from jsonschema import validate
    from backend.agent.single_trade_tools import prepare_single_trade_request
    for name, function in bound.items():
        schema = function['parameters']
        assert not {'actions', 'action', 'contract_version', 'execution_policy', 'depends_on'} & schema['properties'].keys()
        assert schema['additionalProperties'] is False
        for field in schema['properties'].values():
            assert field.get('description'), (mode, field)
        for example in schema['examples']:
            validate(example, schema)
            prepare_single_trade_request(name, example, cfg, cfg['symbol'])
            assert json.dumps(example, ensure_ascii=False, separators=(',', ':')) in function['description']


@pytest.mark.parametrize('style', ['combined_action', 'separate_aliases', 'canonical'])
def test_recorded_three_entry_calls_report_all_spelling_corrections_without_side_effects(monkeypatch, style):
    cfg = {**CFG, 'mode': 'REAL'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    execute = Mock(return_value={'status': 'submitted', 'results': []})
    monkeypatch.setattr('backend.agent.unified_trading.execute_v3_actions', execute)
    actions = []
    for side, price, amount in [('SHORT', 2464.5, .1), ('LONG', 2376.5, .1), ('LONG', 2348.5, .12)]:
        item = {'order_type': 'limit', 'price': price, 'reason': 'offline entry'}
        if style == 'combined_action':
            item.update(action='open_' + side.lower(), quantity=amount)
        elif style == 'separate_aliases':
            item.update(action='open', position_side=side.lower(), quantity=amount)
        else:
            item.update(action='open', side=side, amount=amount)
        actions.append(item)
    args = {'contract_version': 3, 'actions': actions, 'execution_policy': 'independent', 'include': ['account']}
    original = copy.deepcopy(args)
    if style == 'canonical':
        tool_registry.run_trade_tool('execute_trade_actions', args, 'cfg', cfg['symbol'], 'corrected-batch')
        execute.assert_called_once()
        assert execute.call_args.args[0] == original
    else:
        with pytest.raises(ValueError) as caught:
            tool_registry.run_trade_tool('execute_trade_actions', args, 'cfg', cfg['symbol'], 'rejected-batch')
        message = str(caught.value)
        assert all(hint in message for hint in ('第 1、2、3 项', 'side="SHORT"', 'side="LONG"', 'amount', 'quantity', '本批未提交'))
        assert ('action="open"' if style == 'combined_action' else 'position_side') in message
        assert message.count('quantity') == 1 and 'Input tag' not in message and 'Extra inputs' not in message
        execute.assert_not_called()
    assert args == original


def test_open_spelling_hint_retains_other_errors_and_respects_spot_direction():
    cfg = {'mode': 'SPOT_DCA', 'symbol': 'ETH/USDT'}
    actions = [{'action': 'open', 'position_side': 'short', 'order_type': 'limit', 'quantity': .1, 'price': -1}]
    with pytest.raises(ValueError) as caught:
        validate_for_config({'contract_version': 3, 'actions': actions}, cfg)
    message = str(caught.value)
    assert '现货填 side="LONG"' in message and 'SHORT' not in message
    assert 'open.price' in message and 'open.reason' in message


@pytest.mark.parametrize('surface', ['decision', 'chat'])
def test_entry_example_is_in_actual_gemini_compatible_http_request(surface):
    import httpx
    import openai
    from backend.utils.llm_utils import build_chat_model

    cfg = {**CFG, 'mode': 'REAL'}
    tools = chat_graph._get_chat_tools(cfg) if surface == 'chat' else tool_registry.get_trade_tools_for_mode('REAL', cfg)
    captured = []

    def receive(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'offline', 'model': 'gemini-4-argon', 'created': 1,
                              'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': 'stop',
                              'message': {'role': 'assistant', 'content': 'offline'}}]})

    model = build_chat_model(model='gemini-4-argon', api_key='offline', base_url='https://gateway.invalid/v1')
    with httpx.Client(transport=httpx.MockTransport(receive)) as transport:
        model.client = openai.OpenAI(api_key='offline', http_client=transport, max_retries=0).chat.completions
        model.bind_tools(tools).invoke('offline check')
    assert len(captured) == 1
    from backend.agent.single_trade_tools import prepare_single_trade_request
    functions = {tool['function']['name']: tool['function'] for tool in captured[0]['tools']}
    assert set(functions) == {'open_position', 'close_position', 'edit_order', 'cancel_order'}
    for name, function in functions.items():
        example, _ = json.JSONDecoder().raw_decode(function['description'].split('参数示例（按实际意图填写）：', 1)[1])
        assert 'action' not in example and 'actions' not in function['parameters']['properties']
        assert example in function['parameters']['examples']
        prepare_single_trade_request(name, example, cfg, cfg['symbol'])


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


def test_close_with_uppercase_type_requires_explicit_side_without_mutation():
    action = {'action': 'close', 'order_type': 'STOP_MARKET', 'amount': .12,
              'trigger_price': 2349.2, 'reason': 'short stop'}
    payload = {'contract_version': 3, 'actions': [dict(VALID_EDIT), action]}
    original = copy.deepcopy(payload)
    with pytest.raises(ValidationError) as error:
        UnifiedTradeActionsSchema.model_validate(payload)
    details = error.value.errors()
    assert details[0]['loc'] == ('actions', 1, 'close', 'side') and details[0]['type'] == 'missing'
    assert payload == original


@pytest.mark.parametrize('fields,explanation', [({}, '缺少 side'), ({'side': 'SELL'}, 'side 无效'),
                                                  ({'side': None}, 'side 无效')])
def test_chat_rejects_bad_second_direction_before_approval_or_dispatch(monkeypatch, fields, explanation):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    approve = Mock(side_effect=AssertionError('Invalid actions reached approval'))
    dispatch = Mock(side_effect=AssertionError('Invalid batch reached execution'))
    monkeypatch.setattr(chat_graph, 'interrupt', approve)
    monkeypatch.setattr(chat_graph, '_run_tool', dispatch)
    action = {'action': 'close', 'order_type': 'stop_market', 'close_all': True,
              'trigger_price': 2316.5, 'reason': 'short stop', **fields}
    call = {'id': 'invalid-batch', 'name': 'execute_trade_actions', 'args': {
        'contract_version': 3, 'actions': [dict(VALID_EDIT), action]}}
    state = {'messages': [AIMessage(content='', tool_calls=[call])]}
    result = chat_graph.tools_node(state, {'configurable': {'config_id': 'cfg'}})
    message = result['messages'][0].content
    receipt = json.loads(message)
    assert receipt['phase'] == 'preflight' and receipt['submitted'] is False
    assert all(text in receipt['error'] for text in ('第 2 项平仓', explanation, '平多填 LONG，平空填 SHORT', '本批未提交'))
    assert is_transient_schema_error(message)
    approve.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.parametrize('kind,prices', [('MARKET', {}), ('STOP_MARKET', {'trigger_price': 2349.2})])
def test_corrected_close_reaches_execution_with_the_explicit_short_side(monkeypatch, kind, prices):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: dict(CFG))
    execute = Mock(return_value={'status': 'submitted', 'results': []})
    monkeypatch.setattr('backend.agent.unified_trading.execute_v3_actions', execute)
    action = {'action': 'close', 'side': 'SHORT', 'order_type': kind, 'amount': .12,
              'reason': 'close short', **prices}
    payload = {'contract_version': 3, 'actions': [dict(VALID_EDIT), action]}
    canonical = {**action, 'order_type': kind.lower()}
    assert UnifiedTradeActionsSchema.model_validate(payload).model_dump(exclude_none=True)['actions'] == [VALID_EDIT, canonical]
    result = json.loads(tool_registry.run_trade_tool('execute_trade_actions', payload, 'cfg', 'ETH/USDT', 'correct-close'))
    assert result['status'] == 'submitted'
    execute.assert_called_once()
    assert execute.call_args.args[0]['actions'] == [VALID_EDIT, canonical]
    assert action['order_type'] == kind


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('kind,prices', [
    ('market', {}), ('limit', {'price': 2300}), ('stop_market', {'trigger_price': 2349.2}),
    ('stop_limit', {'price': 2350, 'trigger_price': 2349.2}),
    ('take_profit_market', {'trigger_price': 2200}), ('take_profit_limit', {'trigger_price': 2200, 'price': 2201}),
])
def test_order_type_casing_has_the_same_canonical_intent(version, kind, prices):
    action = {'action': 'close', 'side': 'SHORT', 'order_type': kind, 'amount': .12, 'reason': 'exit', **prices}
    payload = {'contract_version': version, 'actions': [action]}
    expected = TradeActionsSchema.model_validate(payload).model_dump(exclude_none=True)
    for spelling in (kind.upper(), kind.title()):
        alternate = {**payload, 'actions': [{**action, 'order_type': spelling}]}
        original = copy.deepcopy(alternate)
        assert TradeActionsSchema.model_validate(alternate).model_dump(exclude_none=True) == expected
        assert alternate == original


@pytest.mark.parametrize('kind', ['TRAILING_STOP_MARKET', 'STOP', '', 42, None])
def test_casing_support_does_not_admit_unknown_types(kind):
    with pytest.raises(ValidationError):
        UnifiedTradeActionsSchema.model_validate({'contract_version': 3, 'actions': [
            {'action': 'close', 'side': 'SHORT', 'order_type': kind, 'amount': .12, 'reason': 'exit'}]})


def test_casing_support_keeps_real_market_entry_restricted():
    with pytest.raises(ValueError, match='第 1 项开仓不支持 order_type=market.*只支持限价开仓.*price.*本批未提交'):
        validate_for_config({'contract_version': 3, 'actions': [
            {'action': 'open', 'side': 'SHORT', 'order_type': 'MARKET', 'amount': .12, 'reason': 'entry'}]}, CFG)


def test_paper_schema_keeps_close_direction_and_lowercase_type_guidance():
    schema = unified_schema_for_config({'mode': 'PAPER'}, engine='paper')
    close = schema['$defs']['UnifiedCloseAction']
    assert {'side', 'order_type'} <= set(close['required'])
    assert 'SHORT closes a short' in close['properties']['side']['description']
    assert 'lowercase' in close['properties']['order_type']['description']
