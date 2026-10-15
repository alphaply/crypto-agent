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
def contracts(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'tool-contracts.sqlite'))
    with database.get_db_conn() as conn:
        from backend.database_schema import initialize_schema
        pass  # PostgreSQL migrations are installed by financial_pg.
        conn.commit()
    with database.get_db_conn() as conn:
        pass  # PostgreSQL migrations are installed by financial_pg.
    monkeypatch.setattr(cfg, 'exchange_profile', lambda name: {
        'profile_id': name, 'exchange': 'binance', 'market_type': 'spot' if name == 'spot-account' else 'swap',
        'api_key': 'offline-test-key', 'secret': 'offline-test-secret',
    })
    cfg.save_profile(cfg.MCPProfile(profile_id='spot', name='Spot', market_type='spot', exchange_profile_id='spot-account',
                                   symbols=['BTC/USDT', 'ETH/USDT'], position_management_policy='strict_owned'))
    cfg.save_profile(cfg.MCPProfile(profile_id='swap', name='Swap', market_type='swap', exchange_profile_id='swap-account',
                                   symbols=['BTC/USDT', 'ETH/USDT:USDT'], position_management_policy='strict_owned'))
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
    result = asyncio.run(server._tool_manager.call_tool(name, arguments))
    if hasattr(result, 'structuredContent'):
        assert not result.isError, result
        return result.structuredContent or json.loads(result.content[0].text)
    return result


def spot_arguments():
    return {'orders': [{'action': 'BUY_LIMIT', 'amount': 1, 'entry_price': 10, 'reason': 'approved test'}]}


def enable_independent_profile():
    profile = store.get('profile', 'swap')
    store.put('profile', 'swap', {**profile, 'exit_mode': 'independent_exits'})


def test_canonical_mcp_tools_use_same_v2_payload_and_persist_version(contracts):
    _, _, dispatch, server = contracts
    enable_independent_profile()
    request = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG',
               'order_type': 'take_profit_market', 'close_all': True, 'trigger_price': 12,
               'reason': 'final remainder', 'operation_id': 'canonical-all-0001'}
    first = call(server, 'close', request)
    assert first['status'] == 'submitted'
    assert dispatch.call_args.args[:2] == ('close', {
        'side': 'LONG', 'order_type': 'take_profit_market', 'reason': 'final remainder',
        'close_all': True, 'trigger_price': 12.0,
    })
    # The legacy dispatcher calling the SAME canonical action shares a receipt.
    assert call(server, 'execute_trade', {
        'profile_id': 'swap', 'symbol': request['symbol'], 'operation_id': request['operation_id'],
        'tool_name': 'close', 'arguments': {key: value for key, value in request.items() if key not in {'profile_id', 'symbol', 'operation_id'}},
    }) == first
    dispatch.assert_called_once()
    with store.connection() as conn:
        row = conn.execute('SELECT contract_version,request_json FROM mcp_operation_requests WHERE operation_id=%s',
                           (request['operation_id'],)).fetchone()
    assert row['contract_version'] == 2 and json.loads(row['request_json'])['contract_version'] == 2


def test_canonical_mcp_rejects_shapes_before_dispatch_and_preserves_legacy_tp(contracts):
    _, _, dispatch, server = contracts
    enable_independent_profile()
    base = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG',
            'order_type': 'take_profit_limit', 'amount': .2, 'price': 12, 'reason': 'target', 'operation_id': 'invalid-new-tp-0001'}
    assert call(server, 'close', base)['status'] == 'failed'  # No trigger must not select v1.
    invalid_all = {**base, 'amount': None, 'close_all': True, 'trigger_price': 12}
    result = call(server, 'close', invalid_all)
    assert result['status'] == 'failed' and result['error_code'] == 'unsupported_combination'
    for bad in ({**base, 'amount': True}, {**base, 'amount': 0}, {**base, 'unknown': 1},
                {**base, 'close_all': 1}, {**base, 'price': '12'}):
        with pytest.raises(ToolError):
            call(server, 'close', bad)
    dispatch.assert_not_called()
    legacy = {'profile_id': 'swap', 'symbol': base['symbol'], 'pos_side': 'LONG', 'exit_type': 'take_profit_limit',
              'amount': .2, 'price': 12, 'reason': 'legacy target', 'operation_id': 'legacy-limit-0001'}
    assert call(server, 'close_perpetual', legacy)['status'] == 'submitted'
    assert dispatch.call_args.args[0] == 'close_position_real'
    assert dispatch.call_args.args[1]['orders'][0]['exit_type'] == 'take_profit_limit'
    assert 'trigger_price' not in dispatch.call_args.args[1]['orders'][0] or dispatch.call_args.args[1]['orders'][0]['trigger_price'] is None


def test_new_tools_cannot_bypass_profile_mode_or_cancel_scope(contracts):
    caller, _, dispatch, server = contracts
    request = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'amount': .2,
               'price': 10, 'reason': 'entry', 'operation_id': 'wrong-mode-0001'}
    assert call(server, 'open', request)['status'] == 'failed'
    dispatch.assert_not_called()
    enable_independent_profile()
    no_cancel = {**caller, 'scopes': ['read', 'trade']}
    actions = [{'action': 'open', 'side': 'LONG', 'amount': .2, 'price': 10, 'reason': 'entry'},
               {'action': 'cancel', 'order_id': 'regular:1', 'reason': 'cancel'}]
    mixed = service.execute(no_cancel, 'swap', request['symbol'], 'execute_trade_actions',
                            {'contract_version': 2, 'actions': actions}, 'mixed-scope-0001')
    assert mixed['status'] == 'failed' and 'cancel' in mixed['error']
    dispatch.assert_not_called()
    cancel_only = {**caller, 'scopes': ['read', 'cancel']}
    assert service.execute(cancel_only, 'swap', request['symbol'], 'execute_trade_actions',
                           {'contract_version': 2, 'actions': actions[1:]}, 'cancel-only-0001')['status'] == 'submitted'


def test_edit_role_is_owned_not_supplied_and_replay_needs_no_live_target(contracts, monkeypatch):
    from backend.agent import independent_tools
    caller, exchange, dispatch, server = contracts
    enable_independent_profile()
    describe = Mock(return_value={'kind': 'exit'})
    monkeypatch.setattr(independent_tools, 'describe_order', describe)
    profile = store.get('profile', 'swap')
    store.put('profile', 'swap', {**profile, 'symbol_scope': 'all', 'symbols': []})
    exchange.markets['ETH/USDT:USDT']['active'] = False
    args = {'order_id': 'algo:123', 'trigger_price': 9, 'reason': 'exit change'}
    no_cancel = {**caller, 'scopes': ['read', 'trade']}
    first = service.execute(no_cancel, 'swap', 'ETH/USDT:USDT', 'edit', args, 'edit-exit-0001')
    assert first['status'] == 'submitted'
    describe.side_effect = ValueError('target no longer live')
    assert service.execute(no_cancel, 'swap', 'ETH/USDT:USDT', 'edit', args, 'edit-exit-0001') == first
    assert describe.call_count == 1 and dispatch.call_count == 1
    describe.side_effect = None
    describe.return_value = {'kind': 'entry'}
    denied = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'edit', {'order_id': 'regular:1', 'price': 10, 'reason': 'entry'}, 'edit-entry-0001')
    assert denied['status'] == 'failed' and dispatch.call_count == 1
    rejected_hint = service.execute(caller, 'swap', 'ETH/USDT:USDT', 'edit', {**args, 'kind': 'exit'}, 'edit-fake-0001')
    assert rejected_hint['status'] == 'failed'


def test_flat_batch_mcp_publishes_v2_and_rejects_legacy_nested_action(contracts):
    _, _, dispatch, server = contracts
    enable_independent_profile()
    args = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'operation_id': 'flat-batch-0001', 'contract_version': 2,
            'actions': [{'action': 'open', 'side': 'LONG', 'amount': .2, 'price': 10, 'reason': 'stage'}]}
    assert call(server, 'execute_trade_actions', args)['status'] == 'submitted'
    assert dispatch.call_args.args[1]['contract_version'] == 2
    assert 'order' not in dispatch.call_args.args[1]['actions'][0]
    with pytest.raises(ToolError):
        call(server, 'execute_trade_actions', {**args, 'actions': [{'action': 'open', 'order': {'amount': .2}}]})
    dispatch.assert_called_once()


def test_flat_mcp_batch_preserves_policy_dependencies_and_replay(contracts):
    _, _, dispatch, server = contracts
    enable_independent_profile()
    args = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'operation_id': 'independent-batch-0001',
            'contract_version': 2, 'execution_policy': 'independent', 'actions': [
                {'action': 'cancel', 'order_id': 'algo:123', 'reason': 'replace'},
                {'action': 'close', 'side': 'LONG', 'order_type': 'stop_market', 'amount': .2,
                 'trigger_price': 9, 'reason': 'new stop', 'depends_on': [0]}]}
    result = call(server, 'execute_trade_actions', args)
    assert result['status'] == 'submitted'
    assert dispatch.call_args.args[1]['execution_policy'] == 'independent'
    assert dispatch.call_args.args[1]['actions'][1]['depends_on'] == [0]
    assert call(server, 'execute_trade_actions', args) == result
    dispatch.assert_called_once()
    with store.connection() as conn:
        row = conn.execute('SELECT request_json FROM mcp_operation_requests WHERE operation_id=%s', (args['operation_id'],)).fetchone()
    stored = json.loads(row['request_json'])['arguments']
    assert stored['execution_policy'] == 'independent' and stored['actions'][1]['depends_on'] == [0]
    with pytest.raises(ToolError):
        call(server, 'execute_trade_actions', {**args, 'execution_policy': 'unrecognized'})


def test_legacy_batch_fingerprint_keeps_original_recursive_defaults(contracts):
    caller, _, dispatch, _ = contracts
    enable_independent_profile()
    action = {'action': 'close', 'order': {'pos_side': 'LONG', 'exit_type': 'take_profit_limit', 'price': 12, 'amount': .2, 'reason': 'legacy'}}
    # This is the request encoded by the pre-upgrade typed legacy schema.
    expected = {'actions': [{'action': 'close', 'order': {
        'action': 'CLOSE', 'pos_side': 'LONG', 'entry_price': 0, 'exit_type': 'take_profit_limit',
        'price': 12.0, 'amount': .2, 'reason': 'legacy',
    }}]}
    assert service.validate_trade_arguments('execute_trade_actions', {'actions': [action]}) == expected
    request = {'symbol': 'ETH/USDT:USDT', 'tool': 'execute_trade_actions', 'arguments': expected}
    old_receipt = {'status': 'submitted', 'operation_id': 'old-batch-replay', 'result': {'legacy': True}}
    store.reserve_operation('swap', 'old-batch-replay', caller['id'], request)
    store.complete_operation('swap', 'old-batch-replay', old_receipt)
    assert service.execute(caller, 'swap', request['symbol'], request['tool'], {'actions': [action]}, 'old-batch-replay') == old_receipt
    dispatch.assert_not_called()
    with pytest.raises(ValueError, match='Unsupported'):
        service.validate_trade_arguments('execute_trade_actions', {'actions': [{**action, 'order': {**action['order'], 'amount_usdt': 10}}]})


@pytest.mark.parametrize('child_filled,child_status', [(0, 'triggered'), (.1, 'partially_filled')])
def test_independent_orders_read_merges_namespaced_owned_and_external_orders_without_writes(contracts, monkeypatch, child_filled, child_status):
    from backend.utils import independent_exits
    caller, exchange, dispatch, _ = contracts
    enable_independent_profile()
    regular = [
        {'id': '1', 'status': 'open', 'filled': 0, 'amount': .2, 'info': {'secret': 'hidden'}},
        {'id': '2', 'status': 'open', 'filled': 0, 'amount': .1},
        {'id': '8', 'status': 'open', 'filled': child_filled, 'amount': .3},
    ]
    algo = [{'id': '1', 'status': 'open', 'amount': None}, {'id': '7', 'status': 'open', 'amount': .3}]
    fetch = Mock(side_effect=lambda symbol, limit, params: algo if params.get('trigger') else regular)
    exchange.fetch_open_orders = fetch
    exchange.create_order = Mock(side_effect=AssertionError('read must never create'))
    exchange.cancel_order = Mock(side_effect=AssertionError('read must never cancel'))
    exchange.edit_order = Mock(side_effect=AssertionError('read must never edit'))
    context = {'entries': [
        {'id': '1', 'order_id': 'regular:1', 'exchange_channel': 'regular', 'side': 'LONG',
         'quantity_mode': 'fixed', 'remaining_amount': .2, 'order_status': 'open'},
        {'id': '4', 'order_id': 'regular:4', 'exchange_channel': 'regular', 'side': 'LONG',
         'quantity_mode': 'fixed', 'remaining_amount': .1, 'order_status': 'open'},
    ], 'exits': [
        {'id': '1', 'order_id': 'algo:1', 'exchange_channel': 'algo', 'side': 'LONG', 'order_type': 'stop_market',
         'quantity_mode': 'all', 'amount': None, 'remaining_amount': None, 'order_status': 'waiting_trigger', 'coverage_estimate': .5},
        {'id': '7', 'execution_order_id': '8', 'order_id': 'regular:8', 'parent_order_id': 'algo:7', 'exchange_channel': 'regular',
         'side': 'LONG', 'order_type': 'take_profit_limit', 'quantity_mode': 'fixed', 'order_status': 'triggered', 'remaining_amount': .3 - child_filled},
    ], 'uncovered': {'LONG': 0, 'SHORT': 0}, 'quantity_conflict': True,
       'quantity_conflicts': [{'side': 'LONG', 'group': 'take_profit', 'excess_amount': .1}],
       'exit_groups': {'LONG': {'take_profit': {'fixed_remaining': .6, 'position_amount': .5}}}, 'pending': False}
    readonly = SimpleNamespace(snapshot=Mock(return_value=context), reconcile_all=Mock(side_effect=AssertionError('no recovery writes')))
    monkeypatch.setattr(independent_exits, 'IndependentExits', lambda _: readonly)
    result = service.query({**caller, 'scopes': ['read']}, 'orders', profile_id='swap', symbol='ETH/USDT:USDT')
    indexed = {row['order_id']: row for row in result}
    assert set(indexed) == {'regular:1', 'regular:2', 'regular:4', 'algo:1', 'regular:8'}
    assert indexed['regular:1']['kind'] == 'entry' and indexed['regular:1']['modifiable'] is True
    assert indexed['algo:1']['quantity_mode'] == 'all' and indexed['algo:1']['amount'] is None
    assert indexed['algo:1']['order_status'] == 'waiting_trigger'
    assert indexed['regular:8']['order_status'] == child_status
    assert indexed['regular:2']['ownership'] == 'external_or_untracked' and indexed['regular:2']['modifiable'] is False
    assert indexed['regular:4']['order_status'] == 'unknown' and indexed['regular:4']['pending'] is True
    assert indexed['regular:4']['modifiable'] is False
    assert indexed['regular:4']['uncovered']['LONG'] is None
    assert indexed['algo:1']['quantity_conflict'] is True
    assert all('info' not in row for row in result)
    assert fetch.call_count == 2
    for writer in (exchange.create_order, exchange.cancel_order, exchange.edit_order, readonly.reconcile_all, dispatch):
        writer.assert_not_called()


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
    from backend.mcp.canonical_tools import CORE_NAMES
    assert {item.name for item in listing} == set(CORE_NAMES)
    # Cached legacy calls retain their original strict flat contracts even
    # though the default model directory only advertises the canonical facade.
    schemas = {item['name']: server._tool_manager.get_tool(item['name']).parameters for item in TOOL_CATALOG}
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


def test_legacy_spot_exit_gateway_uses_ack_only_context(contracts,monkeypatch):
    from backend.utils.trade_operations import financial_ack_only
    _,_,_,server=contracts
    def execute(*args):
        assert financial_ack_only.get() is True
        return {'status':'open','exit_id':'original-exit','filled':None}
    monkeypatch.setattr(spot_orders,'execute',execute)
    result=call(server,'create_spot_exit',{'profile_id':'spot','symbol':'BTC/USDT','amount':1,
                'exit_type':'market','reason':'requested','operation_id':'legacy-ack-once'})
    assert result['status']=='submitted'
    assert financial_ack_only.get() is False


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
            if action == 'CANCEL':
                return {'id':arguments['cancel_order_id'],'status':'canceled','filled':0}
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


def test_cached_mcp_get_operation_is_unregistered_and_internal_reader_is_scoped(contracts):
    from mcp.server.fastmcp.exceptions import ToolError
    caller, _, _, server = contracts
    with pytest.raises(ToolError,match='Unknown tool'):
        call(server, 'get_operation', {'profile_id':'spot','operation_id':'non-existent-op-0001'})
    result = service.operation(caller,'spot','non-existent-op-0001')
    assert result['state'] == 'not_found'
    assert result['found'] is False
    assert result['error_code'] == 'operation_not_found'
    assert 'Operation not found' in result['message']


def test_mcp_spot_inventory_tolerates_preexisting_balance_and_bnb_fees(contracts, monkeypatch):
    _, exchange, _, server = contracts
    monkeypatch.setattr(spot_orders, '_tool', lambda config_id, symbol: (SimpleNamespace(config_id=config_id, exchange=exchange), exchange.market(symbol)))

    database.save_order_log('buy-bnb-fee-1', 'BTC/USDT', 'MCP', 'buy', 100, 0, 0, 'owned buy',
                            trade_mode='SPOT_DCA', config_id='mcp:spot', amount=2, status='FILLED')

    exchange.fetch_balance = lambda: {'BTC': {'total': 10.0, 'free': 10.0, 'used': 0.0}}
    exchange.fetch_my_trades = lambda *args, **kwargs: [{'id': 'fill-bnb-1', 'order': 'buy-bnb-fee-1', 'symbol': 'BTC/USDT', 'side': 'buy', 'amount': 2, 'fee': {'cost': 0.005, 'currency': 'BNB'}}]
    exchange.fetch_order = lambda order_id, symbol, params=None: {'id': order_id, 'symbol': symbol, 'side': 'buy', 'status': 'closed', 'amount': 2, 'filled': 2}
    exchange.id = 'binance'
    exchange.market_info = {'orderTypes': ['MARKET', 'LIMIT', 'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'], 'ocoAllowed': True}
    exchange.amount_to_precision = lambda symbol, amount: str(amount)
    exchange.price_to_precision = lambda symbol, price: str(price)

    result = call(server, 'get_spot_inventory', {'profile_id': 'spot', 'symbol': 'BTC/USDT'})
    assert result['symbol'] == 'BTC/USDT'
    assert result['owned_quantity'] == 2.0
    assert result['untracked_holdings'] == 8.0
    assert result['available_quantity'] == 2.0
    assert result['exchange_free'] == 10.0


def test_mcp_create_spot_exit_bounded_by_mcp_owned_with_preexisting_holdings(contracts, monkeypatch):
    _, exchange, _, server = contracts
    monkeypatch.setattr(spot_orders, '_tool', lambda config_id, symbol: (SimpleNamespace(config_id=config_id, exchange=exchange), exchange.market(symbol)))

    database.save_order_log('buy-exit-coexist-1', 'BTC/USDT', 'MCP', 'buy', 100, 0, 0, 'owned buy',
                            trade_mode='SPOT_DCA', config_id='mcp:spot', amount=3, status='FILLED')

    exchange.fetch_balance = lambda: {'BTC': {'total': 15.0, 'free': 15.0, 'used': 0.0}}
    exchange.fetch_my_trades = lambda *args, **kwargs: [{'id': 'fill-exit-1', 'order': 'buy-exit-coexist-1', 'symbol': 'BTC/USDT', 'side': 'buy', 'amount': 3, 'fee': {'cost': 0.003, 'currency': 'BTC'}}]
    orders = {
        'buy-exit-coexist-1': {'id': 'buy-exit-coexist-1', 'symbol': 'BTC/USDT', 'side': 'buy', 'status': 'closed', 'amount': 3, 'filled': 3},
        'exchange-exit-1': {'id': 'exchange-exit-1', 'symbol': 'BTC/USDT', 'side': 'sell', 'status': 'open', 'amount': 2, 'filled': 0},
    }
    exchange.fetch_order = lambda order_id, symbol, params=None: orders.get(order_id, {'id': order_id, 'symbol': symbol, 'side': 'sell', 'status': 'open', 'amount': 0, 'filled': 0})
    exchange.id = 'binance'
    exchange.market_info = {'orderTypes': ['MARKET', 'LIMIT', 'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'], 'ocoAllowed': True}
    exchange.markets['BTC/USDT']['info'] = exchange.market_info
    exchange.markets['BTC/USDT']['id'] = 'BTCUSDT'
    exchange.amount_to_precision = lambda symbol, amount: str(amount)
    exchange.price_to_precision = lambda symbol, price: str(price)
    exchange.create_order = lambda symbol, kind, side, amount, price, params: {'id': 'exchange-exit-1'}

    exit_result = call(server, 'create_spot_exit', {
        'profile_id': 'spot',
        'symbol': 'BTC/USDT',
        'amount': 2,
        'exit_type': 'limit',
        'price': 105,
        'reason': 'take profit',
        'operation_id': 'spot-exit-coexist-0001'
    })
    assert exit_result['status'] in {'open', 'submitted'}

    oversell = call(server, 'create_spot_exit', {
        'profile_id': 'spot',
        'symbol': 'BTC/USDT',
        'amount': 5,
        'exit_type': 'limit',
        'price': 105,
        'reason': 'take profit',
        'operation_id': 'spot-exit-oversell-0002'
    })
    assert oversell['status'] == 'failed'
    assert 'exceeds verified unreserved MCP-owned inventory' in oversell.get('error', '')


def test_mcp_get_order_and_get_trade_history_read_only_contracts(contracts):
    caller, exchange, _, server = contracts

    tools = {t['name']: t for t in TOOL_CATALOG}
    assert tools['get_order']['scope'] == 'read'
    assert tools['get_trade_history']['scope'] == 'read'

    exchange.fetch_order = lambda order_id, symbol, params=None: {
        'id': order_id,
        'symbol': symbol,
        'status': 'closed',
        'filled': 0.5,
        'average': 50000.0,
        'cost': 25000.0,
        'timestamp': 1760000000000,
        'info': {'secret': 'hidden'},
    }
    exchange.fetch_my_trades = lambda symbol, limit=50: [
        {
            'id': 'fill-101',
            'order': 'order-101',
            'symbol': symbol,
            'side': 'buy',
            'amount': 0.5,
            'price': 50000.0,
            'cost': 25000.0,
            'timestamp': 1760000000000,
            'info': {'secret': 'hidden'},
        }
    ]

    order_data = call(server, 'get_order', {'profile_id': 'spot', 'symbol': 'BTC/USDT', 'order_id': 'order-101'})
    assert order_data['id'] == 'order-101'
    assert order_data['symbol'] == 'BTC/USDT'
    assert order_data['status'] == 'closed'
    assert order_data['filled'] == 0.5
    assert order_data['average'] == 50000.0
    assert 'info' not in order_data

    trades = call(server, 'get_trade_history', {'profile_id': 'spot', 'symbol': 'BTC/USDT', 'limit': 10})
    assert len(trades) == 1
    assert trades[0]['id'] == 'fill-101'
    assert trades[0]['side'] == 'buy'
    assert trades[0]['amount'] == 0.5
    assert 'info' not in trades[0]

    read_only_caller = {**caller, 'scopes': ['read']}
    order_read = service.query(read_only_caller, 'order', profile_id='spot', symbol='BTC/USDT', order_id='order-101')
    assert order_read['id'] == 'order-101'
    trades_read = service.query(read_only_caller, 'trade_history', profile_id='spot', symbol='BTC/USDT', limit=5)
    assert len(trades_read) == 1


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
