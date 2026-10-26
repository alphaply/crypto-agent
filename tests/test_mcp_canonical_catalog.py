import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

from test_mcp_paper import create, ledger
from backend.mcp import canonical_tools, paper, service
from backend.mcp.tools import register_tools
from backend.mcp.paper_tools import register_paper_tools
from test_mcp_tool_contracts import contracts, call


def server_for(caller, monkeypatch):
    monkeypatch.setattr(service, 'principal', lambda: caller)
    server = FastMCP('Canonical offline')
    register_tools(server)
    register_paper_tools(server)
    return server


def test_paper_catalog_counts_scopes_and_hidden_compatibility(ledger, monkeypatch):
    caller, _, _ = ledger
    server = server_for(caller, monkeypatch)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == set(canonical_tools.CORE_NAMES + canonical_tools.PAPER_NAMES)
    assert len(names) == 12
    assert server._tool_manager.get_tool('paper_trade') is not None
    readonly = {**caller, 'scopes': ['read']}
    monkeypatch.setattr(service, 'principal', lambda: readonly)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert len(names) == 7
    assert not names & {'execute_trade_actions', 'paper_bank', 'configure_paper_account', 'adjust_paper_position'}


def test_paper_catalog_does_not_publish_live_parameters(ledger, monkeypatch):
    caller, _, _ = ledger
    server = server_for(caller, monkeypatch)
    for tool in asyncio.run(server.list_tools()):
        assert 'profile_id' not in tool.inputSchema.get('properties', {})
    execution = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == 'execute_trade_actions')
    assert execution.inputSchema['properties']['contract_version']['const'] == 3
    assert 'discriminator' in execution.inputSchema['properties']['actions']['items']
    assert 'action (not action_type)' in execution.description
    edit = execution.inputSchema['$defs']['UnifiedEditAction']
    assert 'order_id' in edit['required']
    assert 'query open orders' in edit['properties']['order_id']['description']
    close = execution.inputSchema['$defs']['UnifiedCloseAction']
    assert {'side', 'order_type'} <= set(close['required'])
    assert 'SHORT closes a short' in close['properties']['side']['description']
    assert 'lowercase' in close['properties']['order_type']['description']


@pytest.mark.parametrize('bad_action,hint', [
    ({'action_type': 'edit', 'trigger_price': 2351.8, 'reason': 'edit stop'}, 'action, not action_type'),
    ({'action': 'edit', 'execution_order_id': 'wrong-field', 'trigger_price': 2351.8,
      'reason': 'edit stop'}, 'order_id is required'),
])
def test_paper_sdk_malformed_edit_rejects_whole_batch_with_useful_hint(ledger, monkeypatch, bad_action, hint):
    from unittest.mock import Mock
    from backend.mcp import paper_batch, paper_orders
    caller, _, _ = ledger
    create(caller)
    queued = paper_orders.place(caller, '123456', 'spot', 'BTC/USDT', 'buy', 'limit',
                                'guidance-existing-order', amount=1, price=99)
    server = server_for(caller, monkeypatch)
    dispatch = Mock(side_effect=AssertionError('Malformed batch must not change an order'))
    monkeypatch.setattr(paper_batch, '_dispatch', dispatch)
    args = {'user_id': '123456', 'operation_id': 'guidance-invalid-batch', 'contract_version': 3,
            'actions': [{'action': 'cancel', 'order_id': queued['order']['order_id'], 'reason': 'cancel'}, bad_action]}
    result = call(server, 'execute_trade_actions', args)
    assert result['status'] == 'failed' and hint in str(result)
    dispatch.assert_not_called()
    assert paper_orders.get_orders(caller, '123456', 'open', 100)['orders'][0]['order_id'] == queued['order']['order_id']


@pytest.mark.parametrize('version', [2, 3])
@pytest.mark.parametrize('bad_action,hint', [
    ({'action_type': 'edit', 'trigger_price': 2351.8, 'reason': 'edit stop'}, 'action, not action_type'),
    ({'action': 'edit', 'execution_order_id': 'wrong-field', 'trigger_price': 2351.8,
      'reason': 'edit stop'}, 'order_id is required'),
])
def test_live_sdk_malformed_edit_never_dispatches(contracts, version, bad_action, hint):
    from mcp.server.fastmcp.exceptions import ToolError
    _, _, dispatch, server = contracts
    with pytest.raises(ToolError, match=hint):
        call(server, 'execute_trade_actions', {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT',
             'operation_id': 'guidance-invalid-live', 'contract_version': version, 'actions': [bad_action]})
    dispatch.assert_not_called()


@pytest.mark.parametrize('version', [2, 3])
def test_live_sdk_uppercase_close_is_canonical_and_replay_does_not_resubmit(contracts, version):
    from test_mcp_tool_contracts import enable_independent_profile
    _, _, dispatch, server = contracts
    enable_independent_profile()
    action = {'action': 'close', 'side': 'SHORT', 'order_type': 'STOP_MARKET',
              'amount': .12, 'trigger_price': 2349.2, 'reason': 'short stop'}
    if version == 3:
        action['symbol'] = 'ETH/USDT:USDT'
    request = {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT', 'operation_id': 'uppercase-close-replay',
               'contract_version': version, 'actions': [action]}
    call(server, 'execute_trade_actions', request)
    assert dispatch.call_args.args[1]['actions'][0]['order_type'] == 'stop_market'
    assert dispatch.call_args.args[1]['actions'][0]['side'] == 'SHORT'
    call(server, 'execute_trade_actions', {**request, 'actions': [{**action, 'order_type': 'stop_market'}]})
    dispatch.assert_called_once()


@pytest.mark.parametrize('version', [2, 3])
def test_live_sdk_missing_close_side_rejects_entire_batch_before_any_dispatch(contracts, version):
    from mcp.server.fastmcp.exceptions import ToolError
    from test_mcp_tool_contracts import enable_independent_profile
    _, _, dispatch, server = contracts
    enable_independent_profile()
    with pytest.raises(ToolError, match='close.side is required'):
        call(server, 'execute_trade_actions', {'profile_id': 'swap', 'symbol': 'ETH/USDT:USDT',
             'operation_id': 'close-missing-side', 'contract_version': version, 'actions': [
                 {'action': 'cancel', 'order_id': 'algo:current', 'reason': 'replace stop'},
                 {'action': 'close', 'order_type': 'STOP_MARKET', 'amount': .12,
                  'trigger_price': 2349.2, 'reason': 'short stop'},
             ]})
    dispatch.assert_not_called()


def test_account_facets_and_records_do_not_leak_or_append_full_account(ledger):
    caller, _, _ = ledger
    create(caller)
    view = canonical_tools.account_state(caller, user_id='123456', include=['positions'])
    assert 'balances' not in view and 'orders' not in view
    assert view['positions'] == []
    with pytest.raises(ValueError, match='profile_id'):
        canonical_tools.account_state(caller, profile_id='live', user_id='123456')


def test_live_records_page_keeps_fill_source_and_truthful_next_page(ledger, monkeypatch):
    caller, _, _ = ledger
    live = {**caller, 'key_type': 'live', 'profile_ids': ['swap'], 'scopes': ['read']}
    monkeypatch.setattr(service, 'require', lambda *args: {'market_type': 'swap'})
    calls = []
    trades = [{'id': str(index), 'amount': .1} for index in range(75)]
    def query(who, resource, **args):
        calls.append((resource, args['limit']))
        return trades[:args['limit']]
    monkeypatch.setattr(service, 'query', query)
    first = canonical_tools.records(live, 'fills', profile_id='swap', symbol='BTC/USDT:USDT', limit=50)
    assert len(first['items']) == 50 and first['has_more'] is True
    final = canonical_tools.records(live, 'fills', profile_id='swap', symbol='BTC/USDT:USDT', limit=50, offset=50)
    assert len(final['items']) == 25 and final['has_more'] is False
    assert calls == [('trade_history', 51), ('trade_history', 101)]


def test_paper_order_account_facet_respects_symbol(ledger, monkeypatch):
    from backend.mcp import paper_orders
    caller, _, _ = ledger
    create(caller)
    monkeypatch.setattr(paper_orders, 'get_orders', lambda *args: {'orders': [
        {'symbol': 'BTC/USDT', 'order_id': 'btc'}, {'symbol': 'ETH/USDT', 'order_id': 'eth'}]})
    result = canonical_tools.account_state(caller, user_id='123456', symbol='BTC/USDT', include=['orders'])
    assert result['orders'] == [{'symbol': 'BTC/USDT', 'order_id': 'btc'}]
    assert 'balances' not in result and 'positions' not in result


def test_records_default_compact_retains_facts_and_detail_returns_original(ledger, monkeypatch):
    caller, _, _ = ledger
    live = {**caller, 'key_type': 'live', 'profile_ids': ['swap'], 'scopes': ['read']}
    monkeypatch.setattr(service, 'require', lambda *args: {'market_type': 'swap'})
    raw = {'id': 'target', 'status': 'open', 'amount': 1, 'filled': .4, 'remaining': .6,
           'quantity_unit': 'base', 'fee': {'cost': .01, 'currency': 'USDT'},
           'amount_units': 'contracts', 'contract_size': .1, 'base_amount': .1,
           'observed_at': 123, 'management': {'state': 'owned', 'owner': 'mcp:swap'},
           'exchange_debug': 'x' * 10000, 'account': {'cash': 1000}}
    monkeypatch.setattr(service, 'query', lambda *args, **kwargs: raw)
    short = canonical_tools.records(live, 'order', profile_id='swap', symbol='BTC/USDT:USDT', order_id='regular:target')['order']
    assert short['filled'] == .4 and short['quantity_unit'] == 'base'
    assert short['amount_units'] == 'contracts' and short['contract_size'] == .1 and short['base_amount'] == .1
    assert short['management']['owner'] == 'mcp:swap' and short['fee']['cost'] == .01
    assert 'account' not in short and 'exchange_debug' not in short
    assert canonical_tools.records(live, 'order', profile_id='swap', symbol='BTC/USDT:USDT', order_id='regular:target', detail=True)['order'] == raw


def test_paper_history_compact_keeps_cash_and_fill_observation(ledger):
    caller, _, _ = ledger
    create(caller)
    paper.trade(caller, '123456', 'spot', 'BTC/USDT', 'buy', 1, 'history-filled-purchase')
    compact = canonical_tools.records(caller, 'fills', user_id='123456')['items'][0]
    assert compact['payload']['fill']['amount'] == 1
    assert compact['payload']['fill']['fee'] == .1
    assert compact['payload']['fill']['observed_at']
    assert compact['payload']['cash_after'] == 899.9
    assert 'namespace' not in compact
    full = canonical_tools.records(caller, 'fills', user_id='123456', detail=True)['items'][0]
    assert full['namespace'] == caller['paper_namespace']


def test_paper_configuration_is_explicit_and_never_resets(ledger):
    caller, _, _ = ledger
    create(caller)
    paper.bank(caller, '123456', 'borrow', 100, 'bank-before-config')
    canonical_tools.configure(caller, '123456', 'setup-existing-account', 'create', name='existing')
    updated = canonical_tools.configure(caller, '123456', 'update-account-config', 'update', leverage=5, leaderboard_visible=False)
    assert updated['account']['cash'] == 1100 and updated['account']['debt'] == 100
    assert updated['account']['leverage'] == 5
    assert updated['account']['leaderboard_visible'] is False


def test_live_key_does_not_receive_paper_catalog_and_cancel_only_keeps_executor(ledger, monkeypatch):
    caller, _, _ = ledger
    live = {**caller, 'key_type': 'live', 'profile_ids': ['a', 'b'], 'scopes': ['read', 'cancel']}
    server = server_for(live, monkeypatch)
    monkeypatch.setattr(service, 'list_profiles', lambda who: [{'profile_id': 'a'}, {'profile_id': 'b'}])
    names = [tool.name for tool in asyncio.run(server.list_tools())]
    assert set(names) == set(canonical_tools.CORE_NAMES) and len(names) == 7
    monkeypatch.setattr(service, 'principal', lambda: {**live, 'scopes': ['read']})
    assert len(asyncio.run(server.list_tools())) == 6


def test_paper_sdk_batch_and_recovery_compact_vs_detail(ledger, monkeypatch):
    caller, _, _ = ledger
    create(caller)
    server = server_for(caller, monkeypatch)
    arguments = {'user_id': '123456', 'operation_id': 'sdk-batch-canonical', 'contract_version': 3,
                 'actions': [{'action': 'open', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'side': 'LONG',
                              'order_type': 'market', 'amount': 1, 'reason': 'requested buy'}]}
    result = call(server, 'execute_trade_actions', arguments)
    assert result['status'] == 'completed' and 'account' not in result['results'][0]
    from backend.mcp import paper_batch
    assert server._tool_manager.get_tool('get_operation') is None
    original = paper_batch.operation(caller,'123456',arguments['operation_id'])
    child_original=paper.operation(caller,'123456',original['results'][0]['operation_id'])
    assert child_original['account']['spot'][0]['amount'] == 1
    assert original['status'] == 'completed'


def test_sdk_facade_does_not_coerce_v3_float_before_strict_parser(ledger, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError
    caller, _, _ = ledger
    create(caller)
    server = server_for(caller, monkeypatch)
    with pytest.raises(ToolError, match='integer'):
        asyncio.run(server._tool_manager.call_tool('execute_trade_actions', {
            'user_id': '123456', 'operation_id': 'float-version-is-invalid', 'contract_version': 3.0,
            'actions': [{'action': 'open', 'market_type': 'spot', 'symbol': 'BTC/USDT', 'side': 'LONG',
                         'order_type': 'market', 'amount': 1, 'reason': 'requested buy'}]}))
    assert paper.get_account(caller, '123456')['spot'] == []


def test_all_scope_live_v3_explicit_symbol_and_private_child_recovery(contracts, monkeypatch):
    from backend.mcp import settings, store
    from backend.agent import tool_registry, unified_dispatch
    caller, _, _, server = contracts
    row = store.get('profile', 'swap')
    store.put('profile', 'swap', {**row, 'symbol_scope': 'all', 'symbols': [], 'exit_mode': 'independent_exits'})
    monkeypatch.setattr(tool_registry, 'run_trade_tool', __import__('test_mcp_tool_contracts').real_run_trade_tool)
    effects = []
    def dispatch(action, config_id, symbol, config, operation_id):
        effects.append((action.action, symbol, operation_id))
        return {'status': 'completed', 'order_id': 'regular:verified-1', 'order_status': 'open'}
    monkeypatch.setattr(unified_dispatch, 'dispatch_live_action', dispatch)
    request = {'profile_id': 'swap', 'operation_id': 'all-scope-parent-v3', 'contract_version': 3,
               'actions': [{'action': 'open', 'symbol': 'ETH/USDT:USDT', 'side': 'LONG',
                            'order_type': 'limit', 'price': 10, 'amount': .1, 'reason': 'requested entry'}]}
    first = call(server, 'execute_trade_actions', request)
    assert first['status'] == 'completed' and len(effects) == 1
    child_id = first['result']['results'][0]['operation_id']
    assert child_id.startswith('mcp-step:')
    assert server._tool_manager.get_tool('get_operation') is None
    recovered = canonical_tools.query_child(caller,'swap',child_id)
    assert recovered['parent_operation_id'] == request['operation_id']
    assert recovered['status'] == 'completed'
    assert call(server, 'execute_trade_actions', request)['status'] == 'completed'
    assert len(effects) == 1
    denied = service.operation({**caller, 'id': 'another-principal'}, 'swap', child_id)
    assert denied['state'] == 'not_found'


def test_canonical_handoff_recovery_is_scoped_and_read_only(contracts, monkeypatch):
    from backend.utils import position_management
    caller, _, dispatch, _ = contracts
    recovery_id = 'handoff:accepted-resource-0001'
    monkeypatch.setattr(position_management, 'lookup_management_recovery', lambda owner, identity: {'query_only': True} if owner == 'mcp:swap' and identity == recovery_id else None)
    calls = []
    def query(owner, identity):
        calls.append((owner, identity))
        return {'operation_id': identity, 'status': 'unknown', 'query': {'status': 'completed'}}
    monkeypatch.setattr(position_management, 'query_management_recovery', query)
    assert service.operation(caller, 'swap', recovery_id)['status'] == 'unknown'
    assert service.operation(caller, 'swap', 'unrelated-private-op')['state'] == 'not_found'
    assert calls == [('mcp:swap', recovery_id)]
    dispatch.assert_not_called()
