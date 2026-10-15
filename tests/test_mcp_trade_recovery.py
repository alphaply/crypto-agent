from types import SimpleNamespace

import pytest

from backend import database
from backend.agent import agent_tools, tool_registry
from backend.mcp import service, store, guard
from backend.utils.independent_exits import IndependentExits
from test_independent_exits import Exchange, entry
from test_mcp_tool_contracts import contracts, enable_independent_profile, real_run_trade_tool
import ccxt
import json


@pytest.mark.parametrize('query_first', [False, True])
def test_mcp_definite_rejection_then_direct_corrected_stop(contracts, monkeypatch, query_first):
    caller, _, _, _ = contracts
    enable_independent_profile()
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='mcp:swap')
    independent = IndependentExits(mt)
    monkeypatch.setattr(guard, 'assert_entry_allowed', lambda *a, **k: None)
    monkeypatch.setattr(guard, 'preflight_tool', lambda *a, **k: None)
    monkeypatch.setattr(service, 'market_tool', lambda _: mt)
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (independent, True))
    monkeypatch.setattr(tool_registry, 'run_trade_tool', real_run_trade_tool)
    opening = independent.open('ETH/USDT', entry(), operation_id='entry', contract_version=2)
    exchange.orders[opening['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    exchange.fetch_my_trades = lambda *a, **k: [{'id': 'entry-fill', 'order': opening['id'], 'amount': 1}]
    independent.reconcile_all()
    create = exchange.create_order
    attempts = []
    def reject(*args, **kwargs):
        attempts.append(1)
        raise ccxt.InvalidOrder('binance {"code":-2021,"msg":"invalid trigger"}')
    exchange.create_order = reject
    args = dict(side='LONG', order_type='stop_market', amount=.2, trigger_price=90, reason='protect')
    first = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', args, 'rejected-stop')
    assert first['status'] == 'failed'
    exchange.create_order = create
    if query_first:
        current = service.operation({**caller, 'scopes': ['read']}, 'swap', 'rejected-stop')
        assert current['result']['result']['query']['status'] == 'completed'
        assert current['result']['result']['status'] == 'failed'
    second = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', {**args, 'trigger_price': 85}, 'corrected-stop')
    assert second['status'] == 'submitted'
    assert len(attempts) == 1 and len(exchange.calls) == 2


def test_mcp_each_blocker_maps_only_within_principal_profile(contracts):
    caller, _, _, _ = contracts
    enable_independent_profile()
    args = dict(side='LONG', amount=.2, price=10, reason='open')
    for operation, principal in [('current-op', caller), ('blocker-op', caller), ('foreign-op', {**caller, 'id': 'other'})]:
        service.execute(principal, 'swap', 'ETH/USDT:USDT', 'open', args, operation)
    ids = {name: 'mcp:' + store.digest(owner + ':' + name) for name, owner in
           [('current-op', caller['id']), ('blocker-op', caller['id']), ('foreign-op', 'other')]}
    payload = {'operation_id': ids['current-op'], 'blockers': [
        {'operation_id': value, 'recovery': {'tool': 'get_trade_operation', 'target_operation_id': value}}
        for value in ids.values()], 'recovery': {'blocker_operation_ids': list(ids.values())}}
    result = service._external_operation_receipt(payload, ids['current-op'], 'current-op', 'swap', principal=caller['id'])
    assert [b['recovery']['target_operation_id'] for b in result['blockers']] == ['current-op', 'blocker-op', None]
    assert result['blockers'][-1]['identity_status'] == 'unavailable_in_caller_scope'
    assert result['recovery']['blocker_operation_ids'] == ['current-op', 'blocker-op']
    assert 'mcp:' not in json.dumps(result)
    cross_profile = service._external_operation_receipt(payload, 'unrelated', 'request', 'spot', principal=caller['id'])
    assert all(b['operation_id'] is None for b in cross_profile['blockers'])


def test_mcp_direct_open_close_edit_cancel_each_need_one_call(contracts, monkeypatch):
    caller, _, _, _ = contracts
    enable_independent_profile()
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='mcp:swap')
    independent = IndependentExits(mt)
    monkeypatch.setattr(guard, 'assert_entry_allowed', lambda *a, **k: None)
    monkeypatch.setattr(guard, 'preflight_tool', lambda *a, **k: None)
    monkeypatch.setattr(service, 'market_tool', lambda _: mt)
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (independent, True))
    monkeypatch.setattr(tool_registry, 'run_trade_tool', real_run_trade_tool)
    counts = {'edit': 0, 'cancel': 0}
    edit, cancel = exchange.edit_order, exchange.cancel_order
    def edit_once(*args, **kwargs):
        counts['edit'] += 1
        return edit(*args, **kwargs)
    def cancel_once(*args, **kwargs):
        counts['cancel'] += 1
        return cancel(*args, **kwargs)
    exchange.edit_order, exchange.cancel_order = edit_once, cancel_once
    def execute(name, args):
        return service.execute(caller, 'swap', 'ETH/USDT:USDT', name, args, 'direct-' + name)
    opening = execute('open', dict(side='LONG', amount=1, price=100, reason='open'))
    assert opening['status'] == 'submitted'
    exchange.orders[opening['result']['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    independent.reconcile_all()
    closing = execute('close', dict(side='LONG', order_type='limit', amount=.2, price=110, reason='exit'))
    assert closing['status'] == 'submitted'
    order_id = closing['result']['order_id']
    assert execute('edit', dict(order_id=order_id, price=111, reason='adjust'))['status'] == 'completed'
    assert execute('cancel', dict(order_id=order_id, reason='remove'))['status'] == 'completed'
    assert len(exchange.calls) == 2 and counts == {'edit': 1, 'cancel': 1}


def test_mcp_read_and_replay_refresh_same_internal_operation_without_writes(contracts, monkeypatch):
    caller, _, _, _ = contracts
    enable_independent_profile()
    exchange = Exchange()
    mt = SimpleNamespace(exchange=exchange, config_id='mcp:swap')
    independent = IndependentExits(mt)
    monkeypatch.setattr(guard, 'assert_entry_allowed', lambda *a, **k: None)
    monkeypatch.setattr(guard, 'preflight_tool', lambda *a, **k: None)
    monkeypatch.setattr(service, 'market_tool', lambda _: mt)
    monkeypatch.setattr(agent_tools, '_independent_service', lambda *_: (independent, True))
    monkeypatch.setattr(tool_registry, 'run_trade_tool', real_run_trade_tool)
    opening = independent.open('ETH/USDT', entry(), operation_id='mcp-entry', contract_version=2)
    exchange.orders[opening['id']].update(status='closed', filled=1)
    exchange.quantity = 1
    independent.reconcile_all()
    args = {'side': 'LONG', 'order_type': 'market', 'close_all': True, 'reason': 'exit'}
    exchange.timeout_next = True
    first = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', args, 'mcp-close-recover')
    assert first['status'] == 'unknown'
    assert first['result']['recovery']['state'] == 'background'
    assert first['result']['recovery']['target_operation_id'] == 'mcp-close-recover'
    for name in ['create_order', 'cancel_order', 'edit_order']:
        monkeypatch.setattr(exchange, name, lambda *a, **k: pytest.fail('read/replay wrote financially'))
    reader = {**caller, 'scopes': ['read']}
    current = service.operation(reader, 'swap', 'mcp-close-recover')
    assert current['state'] == 'completed'
    assert current['result']['result']['filled_amount'] == 1
    replay = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'close', args, 'mcp-close-recover')
    not_found_principal = service.operation({**reader, 'id': 'another-principal'}, 'swap', 'mcp-close-recover')
    assert not_found_principal['state'] == 'not_found' and not_found_principal['found'] is False
    not_found_profile = service.operation(reader, 'spot', 'mcp-close-recover')
    assert not_found_profile['state'] == 'not_found' and not_found_profile['found'] is False
    assert len(exchange.calls) == 2
    with store.connection() as conn:
        link = conn.execute('SELECT * FROM mcp_operation_links WHERE operation_id=%s', ('mcp-close-recover',)).fetchone()
        assert link['principal'] == caller['id']
        assert link['internal_operation_id'] == 'mcp:' + store.digest(caller['id'] + ':mcp-close-recover')
        assert conn.execute('SELECT count(*) FROM mcp_operation_revisions').fetchone()[0] >= 2


def test_mcp_spot_order_recovery_resolves_to_filled(contracts, monkeypatch):
    caller, _, _, _ = contracts
    reader = {**caller, 'scopes': ['read']}

    mock_order = {
        'id': 'spot-12345',
        'status': 'closed',
        'filled': 0.05,
        'average': 68000.0,
        'cost': 3400.0,
        'lastTradeTimestamp': 1760000000000,
    }
    exchange = SimpleNamespace(
        fetch_order=lambda order_id, symbol: mock_order,
        markets={'BTC/USDT': {'symbol': 'BTC/USDT', 'active': True, 'spot': True, 'contract': False}}
    )
    mt = SimpleNamespace(exchange=exchange, config_id='mcp:spot')
    monkeypatch.setattr(service, 'market_tool', lambda _: mt)

    op_id = 'spot-buy-submitted-1'
    request_payload = {
        'symbol': 'BTC/USDT',
        'tool': 'open_position_spot_dca',
        'arguments': {'symbol': 'BTC/USDT', 'amount': 100}
    }
    store.reserve_operation('spot', op_id, caller['id'], request_payload)
    submitted_result = {
        'operation_id': op_id,
        'status': 'submitted',
        'result': {
            'status': 'submitted',
            'order_id': 'spot-12345',
            'symbol': 'BTC/USDT',
            'message': 'Order submitted',
        }
    }
    store.complete_operation('spot', op_id, submitted_result)

    with store.connection() as conn:
        row = conn.execute("SELECT state FROM mcp_operations WHERE profile_id='spot' AND operation_id=%s", (op_id,)).fetchone()
        assert row['state'] == 'submitted'

    result = service.operation(reader, 'spot', op_id)

    assert result['state'] == 'completed'
    res_data = result['result']['result']
    assert res_data['order_status'] == 'FILLED'
    assert res_data['filled_amount'] == 0.05
    assert res_data['avg_fill_price'] == 68000.0
    assert res_data['filled_cost'] == 3400.0
    assert res_data['filled_at'] is not None

    with database.get_db_conn() as conn:
        fill_row = conn.execute("SELECT * FROM spot_order_fills WHERE order_id='spot-12345'").fetchone()
        assert fill_row is not None
        assert fill_row['status'] == 'FILLED'
        assert fill_row['filled_qty'] == 0.05
        assert fill_row['avg_fill_price'] == 68000.0

