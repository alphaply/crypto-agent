"""Canonical schemas and ordered dispatch; all financial effects are intercepted."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend import database
from backend.agent import agent_tools, independent_tools, tool_registry, trade_batch
from backend.agent.trading_contracts import CloseInput, EditInput, OpenInput
from backend.config import config
from backend.database_schema import initialize_schema


@pytest.fixture
def independent(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'independent-contracts.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'STRATEGY', 'exit_mode': 'independent_exits'})
    return 'cfg', 'ETH/USDT'


def request(order_type, **extra):
    return {'side': 'LONG', 'order_type': order_type, 'amount': .2, 'reason': 'stage', **extra}


@pytest.mark.parametrize('order_type,prices', [
    ('market', {}), ('limit', {'price': 99}),
    ('stop_market', {'trigger_price': 90}), ('take_profit_market', {'trigger_price': 120}),
    ('stop_limit', {'trigger_price': 90, 'price': 89}),
    ('take_profit_limit', {'trigger_price': 120, 'price': 119}),
])
def test_six_types_have_exact_price_contracts(order_type, prices):
    assert CloseInput.model_validate(request(order_type, **prices)).order_type == order_type
    for field in {'price', 'trigger_price'} - prices.keys():
        with pytest.raises(ValidationError):
            CloseInput.model_validate(request(order_type, **prices, **{field: 100}))
    for required in prices:
        with pytest.raises(ValidationError):
            CloseInput.model_validate(request(order_type, **{key: value for key, value in prices.items() if key != required}))


@pytest.mark.parametrize('quantity', [
    {}, {'amount': 0}, {'amount': True}, {'amount': '1'}, {'amount': float('inf')},
    {'amount': .2, 'close_all': True}, {'close_all': False}, {'close_all': 1},
])
def test_close_requires_one_strict_quantity_choice(quantity):
    with pytest.raises(ValidationError):
        CloseInput.model_validate({'side': 'SHORT', 'order_type': 'market', 'reason': 'close', **quantity})


def test_full_close_capability_and_no_legacy_type_guess():
    for kind in ('market', 'stop_market', 'take_profit_market'):
        prices = {} if kind == 'market' else {'trigger_price': 100}
        assert CloseInput.model_validate({'side': 'LONG', 'reason': 'all', 'order_type': kind, 'close_all': True, **prices})
    for kind in ('limit', 'stop_limit', 'take_profit_limit'):
        with pytest.raises(ValidationError, match='unsupported_combination'):
            CloseInput.model_validate({'side': 'LONG', 'reason': 'all', 'order_type': kind, 'close_all': True, 'price': 100})
    with pytest.raises(ValidationError, match='trigger_price'):
        CloseInput.model_validate(request('take_profit_limit', price=120))


@pytest.mark.parametrize('model,payload', [
    (OpenInput, dict(side='LONG', amount=True, price=100, reason='x')),
    (OpenInput, dict(side='LONG', amount=1, price=100, reason=' ', stop_loss=90)),
    (EditInput, dict(order_id='regular:1', remaining_amount=0, reason='x')),
    (EditInput, dict(order_id='regular:1', remaining_amount=True, reason='x')),
    (EditInput, dict(order_id='regular:1', reason='x')),
    (EditInput, dict(order_id='regular:1', price=101, side='SHORT', reason='x')),
])
def test_strict_fields_reject_ignored_or_invalid_intent(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_registry_recommends_only_canonical_actions_and_keeps_legacy_execution(independent):
    cfg = {'mode': 'REAL', 'exit_mode': 'independent_exits'}
    tools = tool_registry.get_trade_tools_for_mode('REAL', cfg)
    assert [item.name for item in tools] == ['open', 'close', 'edit', 'cancel', 'adopt_position_real', 'execute_trade_actions']
    batch = tools[-1].args_schema
    assert 'contract_version' in batch['required'] and batch['properties']['contract_version']['const'] == 2
    compatible = {item.name for item in tool_registry.get_trade_tools_for_mode('REAL', cfg, include_legacy=True)}
    assert {'open_position_real', 'close_position_real', 'update_exit_order'} <= compatible
    assert 'update_position_protection_real' not in compatible
    attached = {item.name for item in tool_registry.get_trade_tools_for_mode('REAL', {'mode': 'REAL', 'exit_mode': 'attached_optional'})}
    assert 'open' not in attached and 'open_position_real' in attached


def test_wrapper_forwards_v2_and_all_without_a_quantity(independent, monkeypatch):
    close = Mock(return_value={'status': 'open', 'id': '123', 'order_id': 'algo:123', 'quantity_mode': 'all'})
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (SimpleNamespace(close=close), True))
    cfg_id, symbol = independent
    args = {'side': 'LONG', 'order_type': 'take_profit_market', 'close_all': True, 'trigger_price': 120, 'reason': 'final'}
    receipt = json.loads(tool_registry.run_trade_tool('close', args, cfg_id, symbol, operation_id='final-exit'))
    assert receipt['status'] == 'submitted' and receipt['order_status'] == 'open'
    assert receipt['order_id'] == 'algo:123' and receipt['quantity_mode'] == 'all'
    assert close.call_args.kwargs['contract_version'] == 2
    assert close.call_args.kwargs['amount'] is None and close.call_args.kwargs['close_all'] is True
    assert close.call_args.kwargs['exit_type'] == 'take_profit_market'
    assert json.loads(tool_registry.run_trade_tool('close', args, cfg_id, symbol, operation_id='final-exit')) == receipt
    close.assert_called_once()


def test_context_override_cannot_be_hidden_in_new_action(independent):
    with pytest.raises(ValueError, match='context'):
        tool_registry.run_trade_tool('open', {'side': 'LONG', 'amount': 1, 'price': 100, 'reason': 'x', 'symbol': 'BTC/USDT'}, *independent)


def test_batch_version_is_explicit_and_mixed_shapes_fail_before_writes(independent, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    legacy_open = {'action': 'open', 'order': {'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': .2, 'reason': 'x'}}
    canonical_open = {'action': 'open', 'side': 'LONG', 'price': 100, 'amount': .2, 'reason': 'x'}
    for payload in ({'actions': [canonical_open]}, {'contract_version': 2, 'actions': [legacy_open]},
                    {'contract_version': 2, 'actions': [canonical_open, legacy_open]}):
        with pytest.raises(ValueError):
            tool_registry.run_trade_tool('execute_trade_actions', payload, *independent, operation_id='invalid-batch')
    dispatch.assert_not_called()
    legacy_cancel = trade_batch.TradeActionsSchema.model_validate({'actions': [{'action': 'cancel', 'order_id': '1', 'reason': 'x'}]})
    assert legacy_cancel.contract_version == 1


@pytest.mark.parametrize('failure', ['failed', 'pending', 'unknown'])
def test_v2_batch_stops_replays_and_keeps_per_action_receipts(independent, monkeypatch, failure):
    dispatch = Mock(side_effect=[json.dumps({'status': 'submitted', 'order_status': 'open'}), json.dumps({'status': failure})])
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    actions = [{'action': 'open', 'side': 'LONG', 'amount': .2, 'price': price, 'reason': 'stage'} for price in (100, 99, 98)]
    args = {'contract_version': 2, 'actions': actions}
    first = json.loads(tool_registry.run_trade_tool('execute_trade_actions', args, *independent, operation_id='v2-batch'))
    assert first['results'][0]['status'] == 'submitted'
    assert first['results'][1]['status'] in {'unknown', failure}
    assert first['results'][2]['status'] == 'not_executed' and first['results'][2]['blocked_by_index'] == 1
    assert json.loads(tool_registry.run_trade_tool('execute_trade_actions', args, *independent, operation_id='v2-batch')) == first
    assert dispatch.call_count == 2


def test_one_way_mixed_entry_batch_is_rejected_before_first_write(independent, monkeypatch):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'REAL', 'exit_mode': 'independent_exits'})
    exchange = SimpleNamespace(fetch_position_mode=Mock(return_value={'hedged': False}))
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=exchange))
    dispatch = Mock()
    monkeypatch.setattr(independent_tools, 'execute_action', dispatch)
    actions = [{'action': 'open', 'side': side, 'amount': .2, 'price': 100, 'reason': 'entry'} for side in ('LONG', 'SHORT')]
    rejected = json.loads(trade_batch.execute_trade_actions.func(actions=actions, contract_version=2, config_id=independent[0], symbol=independent[1]))
    assert rejected['status'] == 'failed' and 'One-way' in rejected['error']
    dispatch.assert_not_called()
    exchange.fetch_position_mode.return_value = {'hedged': True}
    dispatch.return_value = json.dumps({'status': 'submitted', 'order_status': 'open'})
    receipt = json.loads(trade_batch.execute_trade_actions.func(actions=actions, contract_version=2, config_id=independent[0], symbol=independent[1]))
    assert receipt['status'] == 'submitted' and dispatch.call_count == 2
