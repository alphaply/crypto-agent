"""Adoption tool boundaries with a fake service and isolated durable receipts."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.agent.agent_models import AdoptPositionRealSchema
from backend.agent.trade_batch import TradeActionsSchema
from backend.config import config
from backend.database_schema import initialize_schema
from backend.utils.independent_exits import IndependentPositionError
from backend.utils import position_adoption
from backend.utils.trade_operations import current_operation_id, tool_result_status


PAYLOAD = {'pos_side': 'SHORT', 'expected_amount': 0.37, 'reason': '用户要求管理手动空仓'}


@pytest.fixture
def adoption_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'adoption-tools.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    state = SimpleNamespace(
        cfg={'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits'},
        clients=[], calls=[], error=None,
    )
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: state.cfg)

    def market_tool(config_id):
        client = SimpleNamespace(config_id=config_id)
        state.clients.append(client)
        return client

    def adopt_position(market_tool, symbol, pos_side, expected_amount, reason, operation_id):
        state.calls.append((market_tool, symbol, pos_side, expected_amount, reason, operation_id))
        if state.error:
            raise state.error
        return {
            'status': 'completed', 'adopted': True, 'symbol': symbol,
            'pos_side': pos_side, 'position_amount': expected_amount,
            'protection_created': False,
        }

    # Isolate the agent/service contract: no account or exchange methods exist here.
    monkeypatch.setattr(position_adoption, 'adopt_position', adopt_position)
    monkeypatch.setattr(agent_tools, 'MarketTool', market_tool)
    return state


def dispatch(payload=None, operation_id='adopt-1'):
    return json.loads(tool_registry.run_trade_tool(
        'adopt_position_real', PAYLOAD if payload is None else payload,
        'cfg', 'ETH/USDT:USDT', operation_id=operation_id,
    ))


def test_adoption_tool_is_real_only_and_not_a_batch_action():
    assert 'adopt_position_real' in {tool.name for tool in tool_registry.get_trade_tools_for_mode('REAL')}
    for mode in ('SPOT_DCA', 'STRATEGY'):
        assert 'adopt_position_real' not in {tool.name for tool in tool_registry.get_trade_tools_for_mode(mode)}
    assert set(agent_tools.adopt_position_real.args_schema.model_fields) == {'pos_side', 'expected_amount', 'reason'}
    with pytest.raises(ValidationError):
        TradeActionsSchema.model_validate({'actions': [{'action': 'adopt', **PAYLOAD}]})


@pytest.mark.parametrize('overrides', [
    {'pos_side': 'SELL'}, {'pos_side': 'short'},
    {'expected_amount': 0}, {'expected_amount': -0.37},
    {'expected_amount': float('nan')}, {'expected_amount': float('inf')},
    {'expected_amount': True}, {'expected_amount': '0.37'},
    {'reason': ''}, {'reason': '  \n\t '}, {'amount': 0.37},
])
def test_invalid_adoption_payload_is_rejected_before_client_creation(adoption_tool, overrides):
    with pytest.raises(ValidationError):
        dispatch({**PAYLOAD, **overrides})
    assert not adoption_tool.clients
    assert not adoption_tool.calls
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM trade_action_runs').fetchone()[0] == 0


@pytest.mark.parametrize('cfg', [
    {'mode': 'REAL', 'exit_mode': 'attached_optional'},
    {'mode': 'REAL', 'exit_mode': 'attached_required'},
    {'mode': 'REAL'},
    {'mode': 'SPOT_DCA', 'exit_mode': 'independent_exits'},
    {'mode': 'STRATEGY', 'exit_mode': 'independent_exits'},
    None,
])
def test_direct_tool_rejects_incompatible_configuration_without_client(adoption_tool, cfg):
    adoption_tool.cfg = cfg
    result = json.loads(agent_tools.adopt_position_real.func(
        **PAYLOAD, config_id='cfg', symbol='ETH/USDT:USDT',
    ))
    assert result['status'] == 'failed'
    assert '独立退出模式' in result['error']
    assert not adoption_tool.clients
    assert not adoption_tool.calls


def test_direct_tool_still_validates_parameters(adoption_tool):
    result = json.loads(agent_tools.adopt_position_real.func(
        **{**PAYLOAD, 'expected_amount': 0}, config_id='cfg', symbol='ETH/USDT:USDT',
    ))
    assert result['status'] == 'failed'
    assert not adoption_tool.clients


def test_dispatch_injects_identity_forwards_operation_and_replays_receipt(adoption_tool):
    payload = {**PAYLOAD, 'reason': '  用户要求管理手动空仓  ',
               'config_id': 'other-task', 'symbol': 'BTC/USDT:USDT'}
    result = dispatch(payload)
    assert result['status'] == 'completed'
    assert result['adopted'] is True
    assert result['protection_created'] is False
    assert 'id' not in result and 'filled' not in result
    assert dispatch(payload) == result
    assert len(adoption_tool.clients) == len(adoption_tool.calls) == 1
    assert adoption_tool.calls[0] == (
        adoption_tool.clients[0], 'ETH/USDT:USDT', 'SHORT', 0.37,
        '用户要求管理手动空仓', 'adopt-1',
    )
    assert adoption_tool.clients[0].config_id == 'cfg'
    assert current_operation_id.get() is None
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT status FROM trade_action_runs').fetchone()[0] == 'completed'
    with pytest.raises(ValueError, match='不同请求'):
        dispatch({**PAYLOAD, 'expected_amount': 0.5})
    assert len(adoption_tool.calls) == 1


@pytest.mark.parametrize('error,status', [
    (ValueError('Position amount changed'), 'failed'),
    (IndependentPositionError('Position belongs to another task',
                              code='position_adoption_conflict',
                              details={'owner_config_id': 'other-task'}), 'failed'),
    (TimeoutError('Position snapshot unavailable'), 'unknown'),
    (RuntimeError('Local write result unavailable'), 'unknown'),
])
def test_adoption_error_classification_and_retry_do_not_repeat_service(adoption_tool, error, status):
    adoption_tool.error = error
    result = dispatch()
    assert result['status'] == tool_result_status(result) == status
    assert result['error'] == str(error)
    if isinstance(error, IndependentPositionError):
        assert result['error_code'] == error.code
        assert result['details'] == error.details
    else:
        assert 'error_code' not in result
    assert dispatch() == result
    assert len(adoption_tool.calls) == 1
    assert current_operation_id.get() is None
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT status FROM trade_action_runs').fetchone()[0] == status


def test_adoption_schema_keeps_reason_explicit():
    with pytest.raises(ValidationError):
        AdoptPositionRealSchema.model_validate({'pos_side': 'SHORT', 'expected_amount': 0.37})
