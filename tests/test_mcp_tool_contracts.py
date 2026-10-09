import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from backend import database
from backend.agent.tool_registry import run_trade_tool as real_run_trade_tool
from backend.database_schema import initialize_schema
from backend.mcp import service, settings as cfg, spot_orders, store
from backend.mcp.tools import TOOL_CATALOG, register_tools


class CatalogExchange:
    def __init__(self):
        self.markets = {}
        for symbol in ('BTC/USDT', 'ETH/USDT', 'ETH/BTC'):
            self.markets[symbol] = {'symbol': symbol, 'active': True, 'spot': True, 'contract': False}
        for symbol in ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDC:USDC'):
            self.markets[symbol] = {'symbol': symbol, 'active': True, 'swap': True, 'linear': True, 'contract': True}
        self.markets['BTC/USD:BTC'] = {'symbol': 'BTC/USD:BTC', 'active': True, 'swap': True, 'linear': False, 'contract': True}
        self.markets['OLD/USDT'] = {'symbol': 'OLD/USDT', 'active': False, 'spot': True}
        self.markets['ETH/USDT:USDT-261225'] = {'symbol': 'ETH/USDT:USDT-261225', 'active': True, 'future': True, 'linear': True}

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_ticker(self, symbol):
        return {'symbol': symbol, 'last': 10, 'info': {'secret': 'never-return'}}

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return [[1, 10, 11, 9, 10, 1]][:limit]


@pytest.fixture
def contracts(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'tool-contracts.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    monkeypatch.setattr(cfg, 'exchange_profile', lambda name: {
        'profile_id': name, 'exchange': 'binance', 'market_type': 'spot' if name == 'spot-account' else 'swap',
        'api_key': 'offline-test-key', 'secret': 'offline-test-secret',
    })
    cfg.save_profile(cfg.MCPProfile(profile_id='spot', name='Spot', market_type='spot', exchange_profile_id='spot-account',
                                   symbols=['BTC/USDT', 'ETH/USDT']))
    cfg.save_profile(cfg.MCPProfile(profile_id='swap', name='Swap', market_type='swap', exchange_profile_id='swap-account',
                                   symbols=['BTC/USDT', 'ETH/USDT:USDT']))
    store.put('settings', 'global', {'enabled': True, 'public_url': 'http://localhost:7860'})
    caller = {'id': 'key:test', 'scopes': ['read', 'trade', 'cancel'], 'profile_ids': ['spot', 'swap']}
    exchange = CatalogExchange()
    monkeypatch.setattr(service, 'market_tool', lambda _: SimpleNamespace(exchange=exchange))
    monkeypatch.setattr(service, 'principal', lambda: caller)
    from backend.agent import tool_registry
    dispatch = Mock(return_value=json.dumps({'status': 'submitted', 'order_id': 'fake-1'}))
    monkeypatch.setattr(tool_registry, 'run_trade_tool', dispatch)
    server = FastMCP('Offline contracts')
    register_tools(server)
    return caller, exchange, dispatch, server


def call(server, name, arguments):
    return asyncio.run(server._tool_manager.call_tool(name, arguments))


def spot_arguments():
    return {'orders': [{'action': 'BUY_LIMIT', 'amount': 1, 'entry_price': 10, 'reason': 'approved test'}]}


def test_symbols_are_canonical_scoped_and_filtered_before_paging(contracts):
    caller, _, dispatch, server = contracts
    first = call(server, 'list_symbols', {'profile_id': 'swap', 'limit': 1})
    second = call(server, 'list_symbols', {'profile_id': 'swap', 'limit': 1, 'offset': 1})
    assert first['total'] == second['total'] == 2
    assert first['symbols'][0]['symbol'] == 'BTC/USDT:USDT' and first['has_more'] is True
    assert second['symbols'][0]['symbol'] == 'ETH/USDT:USDT' and second['has_more'] is False
    assert first['exchange_profile_id'] == 'swap-account' and first['symbol_scope'] == 'selected'
    assert service.query(caller, 'market', profile_id='swap', symbol=first['symbols'][0]['symbol'])['symbol'] == 'BTC/USDT:USDT'
    assert service.query(caller, 'symbols', profile_id='swap', keyword='ETH', quote='USDT')['total'] == 1
    assert service.query(caller, 'symbols', profile_id='spot', keyword='ETH')['symbols'][0]['symbol'] == 'ETH/USDT'
    dispatch.assert_not_called()


def test_all_symbols_never_include_inverse_expiring_or_inactive_markets(contracts):
    caller, _, _, _ = contracts
    for profile_id in ('spot', 'swap'):
        profile = store.get('profile', profile_id)
        store.put('profile', profile_id, {**profile, 'symbol_scope': 'all', 'symbols': []})
    spot = service.query(caller, 'symbols', profile_id='spot')['symbols']
    swap = service.query(caller, 'symbols', profile_id='swap')['symbols']
    assert {row['symbol'] for row in spot} == {'BTC/USDT', 'ETH/USDT', 'ETH/BTC'}
    assert {row['symbol'] for row in swap} == {'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDC:USDC'}
    with pytest.raises(ValueError, match='authorized'):
        service.query({**caller, 'profile_ids': []}, 'symbols', profile_id='spot')


def test_sdk_schema_is_flat_strict_and_discovery_matches_registered_tools(contracts):
    _, _, _, server = contracts
    listing = asyncio.run(server.list_tools())
    assert {item.name for item in listing} == {item['name'] for item in TOOL_CATALOG}
    schemas = {item.name: item.inputSchema for item in listing}
    required = {'profile_id', 'symbol', 'amount', 'entry_price', 'reason', 'operation_id'}
    assert set(schemas['buy_spot']['required']) == required
    assert 'arguments' not in schemas['buy_spot']['properties'] and schemas['buy_spot']['additionalProperties'] is False
    assert schemas['list_symbols']['properties']['limit']['maximum'] == 200
    assert 'stop_limit_price' in schemas['create_spot_exit']['properties']
    assert set(schemas['adopt_perpetual_position']['required']) == {
        'profile_id', 'symbol', 'pos_side', 'expected_amount', 'reason', 'operation_id',
    }


def test_flat_manual_position_adoption_is_scoped_and_uses_durable_receipts(contracts):
    caller, _, dispatch, server = contracts
    profile = store.get('profile', 'swap')
    store.put('profile', 'swap', {**profile, 'exit_mode': 'independent_exits'})
    request = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'pos_side': 'SHORT',
               'expected_amount': .37, 'reason': 'user requested management', 'operation_id': 'adopt-short-0001'}
    result = call(server, 'adopt_perpetual_position', request)
    assert result['status'] == 'submitted'  # Dispatcher is mocked in this contract-only fixture.
    assert call(server, 'adopt_perpetual_position', request) == result
    dispatch.assert_called_once()
    assert dispatch.call_args.args[:4] == ('adopt_position_real', {
        'pos_side': 'SHORT', 'expected_amount': .37, 'reason': 'user requested management',
    }, 'mcp:swap', 'ETH/USDT:USDT')
    for invalid in ({**request, 'expected_amount': True}, {**request, 'amount': .37}):
        with pytest.raises(ToolError):
            call(server, 'adopt_perpetual_position', invalid)
    assert service.execute({**caller, 'scopes': ['read']}, 'swap', request['symbol'], 'adopt_position_real',
                           {'pos_side': 'SHORT', 'expected_amount': .37, 'reason': 'test'},
                           'unauthorized-adopt')['status'] == 'failed'
    assert 'adopt_position_real' not in {item['name'] for item in service.trading_tools('spot')}
    store.put('profile', 'swap', profile)
    assert 'adopt_position_real' not in {item['name'] for item in service.trading_tools('swap')}


def test_flat_and_legacy_json_calls_share_one_operation_receipt(contracts):
    _, _, dispatch, server = contracts
    identity = {'profile_id': 'spot', 'symbol': 'BTC/USDT', 'operation_id': 'same-operation-0001'}
    first = call(server, 'buy_spot', {**identity, 'amount': 1, 'entry_price': 10, 'reason': 'approved test'})
    second = call(server, 'execute_trade', {**identity, 'tool_name': 'open_position_spot_dca', 'arguments': json.dumps({
        'args': {'symbol': 'btc/usdt', 'profile_id': 'spot', 'operation_id': identity['operation_id'],
                 'orders': json.dumps(spot_arguments()['orders'])},
    })})
    assert first == second and first['status'] == 'submitted'
    dispatch.assert_called_once()
    args = dispatch.call_args
    assert args.args[2:4] == ('mcp:spot', 'BTC/USDT')
    assert args.kwargs['operation_id'].startswith('mcp:')


def test_direct_args_envelope_is_accepted_without_relaxing_identity_or_unknown_fields(contracts):
    _, _, dispatch, server = contracts
    request = {'profile_id': 'swap', 'symbol': 'BTC/USDT:USDT', 'side': 'LONG', 'amount': 1,
               'entry_price': 10, 'stop_loss': 9, 'take_profit': 11, 'reason': 'approved test', 'operation_id': 'flat-perpetual-0001'}
    assert call(server, 'open_perpetual', {'args': json.dumps(request)})['status'] == 'submitted'
    assert dispatch.call_args.args[1]['orders'][0]['stop_loss'] == 9
    with pytest.raises(ToolError, match='Conflicting argument field: symbol'):
        call(server, 'open_perpetual', {'symbol': 'ETH/USDT:USDT', 'args': request})
    with pytest.raises(ToolError, match='Extra inputs are not permitted'):
        call(server, 'open_perpetual', {**request, 'leverage': 100})
    with pytest.raises(ToolError, match='boolean'):
        call(server, 'open_perpetual', {**request, 'amount': True})
    assert dispatch.call_count == 1


@pytest.mark.parametrize('injected', [
    {'profile_id': 'swap'}, {'config_id': 'some-other-task'}, {'symbol': 'ETH/USDT'},
    {'operation_id': 'another-operation'}, {'tool_name': 'cancel_orders_real'}, {'cycle_id': 'caller-cycle'},
])
def test_gateway_identity_conflicts_fail_before_any_reservation(contracts, injected):
    caller, _, dispatch, _ = contracts
    result = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca', {**spot_arguments(), **injected}, 'identity-test-0001')
    assert result['status'] == 'failed'
    dispatch.assert_not_called()
    with store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mcp_operations').fetchone()[0] == 0


@pytest.mark.parametrize('arguments', [
    {**spot_arguments(), 'stop_loss': 9},
    {'orders': [{**spot_arguments()['orders'][0], 'take_profit': 20}]},
    {'orders': [{**spot_arguments()['orders'][0], 'quote_amount': 100}]},
    {'orders': [{**spot_arguments()['orders'][0], 'amount': True}]},
    '{"orders":[],"orders":[{"action":"BUY_LIMIT","amount":1,"entry_price":10,"reason":"test"}]}',
    {'args': {'amount': 1}, 'amount': 2},
])
def test_unknown_money_fields_and_conflicts_are_not_silently_dropped(contracts, arguments):
    caller, _, dispatch, _ = contracts
    result = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca', arguments, 'unsupported-0001')
    assert result['status'] == 'failed'
    dispatch.assert_not_called()


def test_reusing_operation_id_for_changed_money_or_another_caller_is_rejected(contracts):
    caller, _, dispatch, _ = contracts
    first = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca', spot_arguments(), 'idempotency-0001')
    changed = spot_arguments()
    changed['orders'][0]['amount'] = 2
    wrong_amount = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca', changed, 'idempotency-0001')
    wrong_caller = service.execute({**caller, 'id': 'key:other'}, 'spot', 'BTC/USDT', 'open_position_spot_dca', spot_arguments(), 'idempotency-0001')
    assert first['status'] == 'submitted' and wrong_amount['status'] == wrong_caller['status'] == 'failed'
    dispatch.assert_called_once()


def test_spot_inventory_and_exit_tools_use_scoped_durable_gateway(contracts, monkeypatch):
    caller, _, dispatch, server = contracts
    inventory = Mock(return_value={'symbol': 'BTC/USDT', 'available': 1})
    exits = Mock(return_value={'status': 'open', 'exit_id': 'exit-offline-1'})
    monkeypatch.setattr(spot_orders, 'inventory', inventory)
    monkeypatch.setattr(spot_orders, 'execute', exits)
    assert call(server, 'get_spot_inventory', {'profile_id': 'spot', 'symbol': 'BTC/USDT'})['available'] == 1
    inventory.assert_called_once_with('mcp:spot', 'BTC/USDT')
    request = {'profile_id': 'spot', 'symbol': 'BTC/USDT', 'amount': 1, 'exit_type': 'oco',
               'stop_loss': 9, 'take_profit': 11, 'reason': 'approved exits', 'operation_id': 'spot-exit-0001'}
    first = call(server, 'create_spot_exit', request)
    assert first['status'] == 'submitted' and first['result']['exit_id'] == 'exit-offline-1'
    assert call(server, 'create_spot_exit', request) == first
    exits.assert_called_once()
    assert exits.call_args.args[2:4] == ('mcp:spot', 'BTC/USDT')
    assert exits.call_args.args[4].startswith('mcp:')
    no_cancel = {**caller, 'scopes': ['read', 'trade']}
    denied = service.execute(no_cancel, 'spot', 'BTC/USDT', 'cancel_spot_exit', {'exit_id': 'exit-offline-1', 'reason': 'cancel'}, 'spot-cancel-0001')
    assert denied['status'] == 'failed' and 'cancel' in denied['error']
    dispatch.assert_not_called()


def test_invalid_exit_prices_fail_before_exit_dispatch(contracts, monkeypatch):
    _, _, _, server = contracts
    exits = Mock()
    monkeypatch.setattr(spot_orders, 'execute', exits)
    result = call(server, 'create_spot_exit', {'profile_id': 'spot', 'symbol': 'BTC/USDT', 'amount': 1,
                                             'exit_type': 'market', 'price': 10, 'reason': 'test', 'operation_id': 'invalid-exit-0001'})
    assert result['status'] == 'failed' and 'not used' in result['error']
    exits.assert_not_called()


def test_sdk_does_not_erase_duplicate_json_money_fields_before_gateway_validation(contracts):
    _, _, dispatch, server = contracts
    result = call(server, 'execute_trade', {
        'profile_id': 'spot', 'symbol': 'BTC/USDT', 'tool_name': 'open_position_spot_dca',
        'operation_id': 'duplicate-money-0001',
        'arguments': '{"orders":[{"action":"BUY_LIMIT","amount":1,"amount":9,"entry_price":10,"reason":"test"}]}',
    })
    assert result['status'] == 'failed' and 'duplicate JSON field' in result['error']
    dispatch.assert_not_called()


def test_flat_cancel_uses_real_spot_dispatcher_and_rejects_foreign_orders(contracts, monkeypatch):
    from backend.agent import agent_tools, tool_registry
    from backend.config import config

    _, _, _, server = contracts
    monkeypatch.setattr(tool_registry, 'run_trade_tool', real_run_trade_tool)
    monkeypatch.setattr(config, 'get_config_by_id', cfg.get_runtime_profile)
    writes = []

    class Market:
        def __init__(self, **kwargs):
            self.exchange = SimpleNamespace(fetch_order=lambda *args: {'status': 'canceled', 'filled': 0})

        def get_account_status(self, symbol, **kwargs):
            return {'available_balance': 1000, 'real_open_orders': []}

        def place_real_order(self, symbol, action, arguments, **kwargs):
            writes.append((symbol, action))
            return {'id': 'exchange-' + str(len(writes))}

    monkeypatch.setattr(agent_tools, 'MarketTool', Market)
    identity = {'profile_id': 'spot', 'symbol': 'ETH/USDT'}
    bought = call(server, 'buy_spot', {**identity, 'amount': 1, 'entry_price': 10,
                                      'reason': 'test', 'operation_id': 'real-flat-buy-0001'})
    assert bought['status'] == 'submitted'
    foreign = call(server, 'cancel_order', {**identity, 'order_id': 'foreign', 'reason': 'test',
                                          'operation_id': 'real-foreign-0001'})
    assert foreign['status'] == 'failed' and len(writes) == 1
    cancelled = call(server, 'cancel_order', {**identity, 'order_id': 'exchange-1', 'reason': 'test',
                                            'operation_id': 'real-cancel-0001'})
    assert cancelled['status'] == 'completed'
    assert writes == [('ETH/USDT', 'BUY_LIMIT'), ('ETH/USDT', 'CANCEL')]
