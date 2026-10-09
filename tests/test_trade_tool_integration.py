"""Exercise public tool dispatch with the real simulated ledger, without an exchange."""
import json
import time
from types import SimpleNamespace

import pytest

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.config import config
from backend.database_independent import MockIndependentTrading
from backend.database_schema import initialize_schema


@pytest.fixture
def simulation(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'tools.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    cfg = {'config_id': 'cfg', 'symbol': 'ETH/USDT', 'mode': 'STRATEGY', 'exit_mode': 'independent_exits'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda cid: cfg if cid == 'cfg' else None)
    monkeypatch.setattr(agent_tools, 'MarketTool', lambda **_: SimpleNamespace(exchange=SimpleNamespace(fetch_ticker=lambda _: {'last': 100})))
    return MockIndependentTrading('cfg', 'ETH/USDT')


def call(name, args, operation_id):
    return json.loads(tool_registry.run_trade_tool(name, args, 'cfg', 'ETH/USDT', operation_id=operation_id))


def test_one_call_multi_exit_and_replay(simulation):
    opening = {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 2, 'reason': 'test'}]}
    first = call('open_position_strategy', opening, 'entry-call')
    assert call('open_position_strategy', opening, 'entry-call') == first
    assert len(simulation.snapshot()['entries']) == 1
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    payload = {'actions': [
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'take_profit_limit', 'price': 120, 'amount': 1, 'reason': 'first target'}},
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'take_profit_limit', 'price': 130, 'amount': 1, 'reason': 'second target'}},
        {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': 0, 'reason': 'invalidation'}},
    ]}
    result = call('execute_trade_actions', payload, 'exit-call')
    assert all(r['status'] == 'submitted' for r in result['results'])
    assert call('execute_trade_actions', payload, 'exit-call') == result
    assert len(simulation.snapshot()['exits']) == 3
    market = {'orders': [{'pos_side': 'LONG', 'exit_type': 'market', 'amount': .5, 'reason': 'reduce'}]}
    call('close_position_strategy', market, 'reduce-call')
    assert sorted(x['remaining'] for x in simulation.snapshot()['exits']) == [.75, .75, 1.5]
    assert simulation.snapshot()['positions'][0]['amount'] == 1.5


def test_amend_exit_dispatch_and_cancel(simulation):
    call('open_position_strategy', {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'test'}]}, 'entry')
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    call('close_position_strategy', {'orders': [{'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': 1, 'reason': 'test'}]}, 'exit')
    order_id = simulation.snapshot()['exits'][0]['order_id']
    updated = call('update_exit_order', {'order_id': order_id, 'trigger_price': 95, 'amount': .5, 'reason': 'revise'}, 'amend')
    assert updated['trigger_price'] == 95 and updated['remaining'] == .5
    call('cancel_orders_strategy', {'order_id': order_id, 'reason': 'cancel'}, 'cancel')
    assert simulation.snapshot()['exits'] == []


def test_existing_protection_api_cannot_write_independent_mode(simulation, monkeypatch):
    from backend.app.services import stats_service
    monkeypatch.setattr(stats_service.global_config, 'get_config_by_id', config.get_config_by_id)
    with pytest.raises(ValueError, match='独立退出'):
        stats_service.update_position_protection_payload('cfg', 'ETH/USDT', 'LONG', stop_loss=90)


def test_stats_show_aggregate_position_and_exit_quantities(simulation, monkeypatch):
    from backend.app.services import stats_service
    monkeypatch.setattr(stats_service, 'MarketTool', agent_tools.MarketTool)
    call('open_position_strategy', {'orders': [{'action': 'BUY_LIMIT', 'entry_price': 100, 'amount': 1, 'reason': 'test'}]}, 'entry')
    simulation.monitor(110, 90, int((time.time() + 60) * 1000))
    call('close_position_strategy', {'orders': [{'pos_side': 'LONG', 'exit_type': 'stop_market', 'trigger_price': 90, 'amount': .5, 'reason': 'test'}]}, 'exit')
    stats = stats_service.get_position_stats_payload('cfg')
    assert stats['positions'][0]['qty'] == 1
    assert stats['exit_management']['mode'] == 'independent_exits'
    assert stats['exit_management']['exits'][0]['remaining'] == .5
    assert stats['exit_management']['uncovered']['LONG'] == .5


def test_legacy_cancel_list_stops_after_unknown_result(monkeypatch):
    calls = []

    def cancel(**kwargs):
        calls.append(kwargs['order_id'])
        return '{"status":"unknown","error":"response lost"}'

    monkeypatch.setitem(tool_registry._TOOL_BY_NAME, 'cancel_orders_strategy', SimpleNamespace(func=cancel))
    result = tool_registry.run_trade_tool('cancel_orders_strategy', ['one', 'two'], 'missing-config', 'ETH/USDT')
    assert calls == ['one']
    assert 'not_executed' in result


def test_legacy_multi_order_reports_unexecuted_after_failure(simulation):
    result = call('close_position_strategy', {'orders': [
        {'pos_side': 'LONG', 'exit_type': 'market', 'amount': 1, 'reason': 'no position'},
        {'pos_side': 'SHORT', 'exit_type': 'market', 'amount': 1, 'reason': 'must not execute'},
    ]}, 'failed-multi-close')
    assert result['status'] == 'failed'
    assert [row['status'] for row in result['results']] == ['failed', 'not_executed']
    assert [row['index'] for row in result['results']] == [0, 1]
    assert result['results'][1]['blocked_by_index'] == 0


@pytest.mark.parametrize('tool_name', ['close_position_real', 'execute_trade_actions'])
def test_real_close_skips_confirmed_empty_side_and_continues_batch(simulation, monkeypatch, tool_name):
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits',
    })
    calls = []

    def close(symbol, **kwargs):
        calls.append((symbol, kwargs['pos_side']))
        if kwargs['pos_side'] == 'LONG':
            return {'status': 'skipped', 'reason': 'no_position', 'symbol': symbol, 'pos_side': 'LONG'}
        return {'status': 'filled', 'id': 'short-exit', 'filled': 1}

    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (SimpleNamespace(close=close), True))
    orders = [
        {'pos_side': side, 'exit_type': 'market', 'amount': 0, 'reason': 'close held positions'}
        for side in ('LONG', 'SHORT')
    ]
    payload = {'orders': orders} if tool_name == 'close_position_real' else {
        'actions': [{'action': 'close', 'order': order} for order in orders],
    }
    result = call(tool_name, payload, 'close-both-sides')
    assert result['status'] == 'completed'
    assert [row['index'] for row in result['results']] == [0, 1]
    assert all('blocked_by_index' not in row for row in result['results'])
    if tool_name == 'close_position_real':
        assert [row['status'] for row in result['results']] == ['skipped', 'filled']
        assert result['results'][0]['reason'] == 'no_position'
    else:
        first = json.loads(result['results'][0]['result'])
        assert first['results'][0]['status'] == 'skipped'
        assert first['results'][0]['reason'] == 'no_position'
    assert calls == [('ETH/USDT', 'LONG'), ('ETH/USDT', 'SHORT')]
    assert call(tool_name, payload, 'close-both-sides') == result
    assert len(calls) == 2


@pytest.mark.parametrize('tool_name', ['close_position_real', 'execute_trade_actions'])
@pytest.mark.parametrize('uncertain', [False, True])
def test_real_close_preserves_diagnostic_and_blocks_following_items(simulation, monkeypatch, tool_name, uncertain):
    from backend.utils.independent_exits import IndependentPositionError

    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {
        'config_id': 'cfg', 'mode': 'REAL', 'exit_mode': 'independent_exits',
    })
    details = {
        'symbol': 'ETH/USDT:USDT', 'pos_side': 'LONG', 'cycle_state': None,
        'position_amount': 1, 'next_action': 'Verify this task and position ownership before retrying.',
    }
    calls = []

    def close(symbol, **kwargs):
        calls.append((symbol, kwargs['pos_side']))
        if uncertain:
            raise TimeoutError('Position query unavailable')
        raise IndependentPositionError(
            'No active independent position cycle',
            code='independent_position_cycle_missing', details=details,
        )

    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (SimpleNamespace(close=close), True))
    orders = [
        {'pos_side': side, 'exit_type': 'market', 'amount': 0, 'reason': 'close held positions'}
        for side in ('LONG', 'SHORT')
    ]
    payload = {'orders': orders} if tool_name == 'close_position_real' else {
        'actions': [{'action': 'close', 'order': order} for order in orders],
    }
    result = call(tool_name, payload, 'blocked-close')
    expected = 'unknown' if uncertain else 'failed'
    assert result['status'] == expected
    assert [row['status'] for row in result['results']] == [expected, 'not_executed']
    assert [row['index'] for row in result['results']] == [0, 1]
    assert result['results'][1]['blocked_by_index'] == 0
    first = result['results'][0]
    if tool_name == 'execute_trade_actions':
        first = json.loads(first['result'])['results'][0]
    if uncertain:
        assert first['error'] == 'Position query unavailable'
        assert 'error_code' not in first
    else:
        assert first['error_code'] == 'independent_position_cycle_missing'
        assert first['details'] == details
    assert calls == [('ETH/USDT', 'LONG')]
    assert call(tool_name, payload, 'blocked-close') == result
    assert len(calls) == 1
