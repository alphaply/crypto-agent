import json
from types import SimpleNamespace

import pytest

from backend import database
from backend.database_schema import initialize_schema
from backend.mcp import service, settings as cfg, store
from backend.mcp.transfer import export_settings, import_settings
from backend.utils.spot_execution import _initialize


class Exchange:
    def __init__(self):
        self.markets = {}
        for symbol in ('BTC/USDT', 'ETH/USDT', 'ETH/BTC', 'SOL/USDC'):
            self.markets[symbol] = {'symbol': symbol, 'spot': True, 'contract': False, 'active': True}
        for symbol in ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDC:USDC'):
            self.markets[symbol] = {'symbol': symbol, 'swap': True, 'contract': True, 'linear': True, 'active': True}
        self.markets['BTC/USD:BTC'] = {'symbol': 'BTC/USD:BTC', 'swap': True, 'contract': True, 'linear': False}
        self.markets['BTC/USDT:USDT-261225'] = {'symbol': 'BTC/USDT:USDT-261225', 'contract': True, 'future': True, 'linear': True}
        self.markets['OLD/USDT'] = {'symbol': 'OLD/USDT', 'spot': True, 'active': False}
        self.position_requests = []

    def market(self, symbol):
        return self.markets[symbol]

    def fetch_balance(self):
        return {'total': {'USDT': 1000}, 'info': {'private': True}}

    def fetch_positions(self, symbols=None):
        self.position_requests.append(symbols)
        return [
            {'symbol': 'ETH/USDT:USDT', 'contracts': 1, 'info': {}},
            {'symbol': 'BTC/USD:BTC', 'contracts': 1},
            {'symbol': 'BTC/USDT:USDT-261225', 'contracts': 1},
        ]


@pytest.fixture
def unrestricted(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'scope.db'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        _initialize(conn)
        conn.executemany("INSERT INTO exchange_profiles(profile_id,name,exchange,market_type,updated_at) VALUES(?,'test','binance',?,'2026-10-09')",
                         [('spot-account', 'spot'), ('swap-account', 'swap')])
        conn.commit()
    monkeypatch.setattr(cfg, 'exchange_profile', lambda name: {
        'profile_id': name, 'exchange': 'binance', 'market_type': name.split('-')[0],
        'api_key': 'fake-key', 'secret': 'fake-secret',
    })
    for market in ('spot', 'swap'):
        cfg.save_profile(cfg.MCPProfile(profile_id=market, name=market, exchange_profile_id=f'{market}-account',
                                        market_type=market, symbol_scope='all', symbols=[]))
    store.put('settings', 'global', {'enabled': True, 'public_url': 'http://localhost:7860'})
    exchange = Exchange()
    monkeypatch.setattr(service, 'market_tool', lambda _: SimpleNamespace(exchange=exchange))
    caller = {'id': 'key:test', 'scopes': ['read', 'trade', 'cancel'], 'profile_ids': ['spot', 'swap']}
    return caller, exchange


def spot_order(symbol=None, amount=1, price=10):
    return {'action': 'BUY_LIMIT', 'entry_price': price, 'amount': amount, 'reason': 'test',
            **({'symbol': symbol} if symbol else {})}


def test_explicit_scope_defaults_and_fresh_profiles(unrestricted):
    caller, _ = unrestricted
    with pytest.raises(ValueError, match='Select at least one'):
        cfg.MCPProfile(profile_id='missing', name='Missing', exchange_profile_id='spot-account', market_type='spot')
    legacy = cfg.MCPProfile(profile_id='legacy', name='Legacy', exchange_profile_id='swap-account', symbols=['ETH/USDT'])
    assert legacy.symbol_scope == 'selected'
    assert cfg.get_runtime_profile('mcp:spot')['symbol'] is None
    assert cfg.maintenance_profiles() == []
    assert service.query(caller, 'balance', profile_id='spot')['total'] == {'USDT': 1000}
    assert any(tool['name'] == 'open_position_spot_dca' for tool in service.query(caller, 'tools', profile_id='spot'))
    assert all(row['symbol_scope'] == 'all' for row in service.list_profiles(caller))


@pytest.mark.parametrize('market,symbol', [
    ('spot', 'ETH/USDT:USDT'), ('spot', 'MISSING/USDT'), ('spot', 'OLD/USDT'),
    ('swap', 'BTC/USD:BTC'), ('swap', 'BTC/USDT:USDT-261225'),
])
def test_unrestricted_validates_real_market_and_type(unrestricted, market, symbol):
    _, exchange = unrestricted
    with pytest.raises(ValueError):
        service.normalize_symbol(store.get('profile', market), symbol, exchange)


def test_selected_aliases_stay_restricted_and_perpetual_queries_filter_other_markets(unrestricted):
    caller, exchange = unrestricted
    profile = {'symbol_scope': 'selected', 'symbols': ['ETH/USDT'], 'market_type': 'swap'}
    assert service.normalize_symbol(profile, 'eth/usdt', exchange) == 'ETH/USDT'
    with pytest.raises(ValueError, match='outside'):
        service.normalize_symbol(profile, 'BTC/USDT', exchange)
    assert service.normalize_symbol(store.get('profile', 'swap'), 'SOL/USDC', exchange) == 'SOL/USDC:USDC'
    positions = service.query(caller, 'positions', profile_id='swap')
    assert [row['symbol'] for row in positions] == ['ETH/USDT:USDT']
    assert exchange.position_requests == [None]
    assert 'info' not in positions[0]


def test_spot_call_context_is_scoped_and_operation_quote_cannot_change_on_replay(unrestricted, monkeypatch):
    from backend.agent import tool_registry
    from backend.utils.spot_portfolio import get_config_symbols
    from backend.utils.spot_config_guard import spot_execution_fingerprint

    caller, _ = unrestricted
    calls = []
    baseline = spot_execution_fingerprint(cfg.get_runtime_profile('mcp:spot'))

    def run(name, args, config_id, symbol, **kwargs):
        runtime = cfg.get_runtime_profile(config_id)
        calls.append((get_config_symbols(runtime), kwargs['cycle_id']))
        assert runtime['dca_amount'] == 100
        assert runtime.get('dca_budget') is None
        assert spot_execution_fingerprint(runtime) == baseline
        assert cfg.get_runtime_profile('mcp:swap')['symbol'] is None
        return '{"status":"submitted"}'

    monkeypatch.setattr(tool_registry, 'run_trade_tool', run)
    first = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca',
                            {'orders': [spot_order('BTC/USDT'), spot_order('ETH/USDT')]}, 'first-operation')
    assert first['status'] == 'submitted'
    second = service.execute(caller, 'spot', 'ETH/BTC', 'open_position_spot_dca',
                             {'orders': [spot_order()]}, 'second-operation')
    assert second['status'] == 'submitted'
    replay = service.execute(caller, 'spot', 'ETH/BTC', 'open_position_spot_dca',
                             {'orders': [spot_order()]}, 'first-operation')
    assert replay['status'] == 'failed'
    assert [symbols for symbols, _ in calls] == [['BTC/USDT', 'ETH/USDT'], ['ETH/BTC']]
    assert calls[0][1] != calls[1][1]
    assert cfg.get_runtime_profile('mcp:spot')['symbols'] == []
    with pytest.raises(RuntimeError):
        with cfg.runtime_symbols('mcp:spot', ['BTC/USDT']):
            raise RuntimeError('interrupted')
    assert cfg.get_runtime_profile('mcp:spot')['symbols'] == []


def test_spot_mixed_quotes_rejected_before_operation_or_budget_reservation(unrestricted, monkeypatch):
    from backend.agent import tool_registry
    caller, _ = unrestricted
    monkeypatch.setattr(tool_registry, 'run_trade_tool', lambda *a, **k: pytest.fail('must not execute'))
    result = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca',
                             {'orders': [spot_order('ETH/BTC')]}, 'mixed-quote-operation')
    assert result['status'] == 'failed'
    with store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mcp_operations').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM spot_budget_cycles').fetchone()[0] == 0


def test_unrestricted_spot_preserves_shared_per_operation_allowance(unrestricted, monkeypatch):
    from backend.agent import tool_registry
    from backend.utils.spot_execution import current_spot_cycle_id, reserve_spot_batch
    caller, _ = unrestricted

    def reserve(name, args, config_id, symbol, **kwargs):
        token = current_spot_cycle_id.set(kwargs['cycle_id'])
        try:
            reserve_spot_batch(config_id, cfg.get_runtime_profile(config_id), 'batch',
                               [SimpleNamespace(**order) for order in args['orders']])
        finally:
            current_spot_cycle_id.reset(token)
        return '{"status":"submitted"}'

    monkeypatch.setattr(tool_registry, 'run_trade_tool', reserve)
    result = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca',
                             {'orders': [spot_order('BTC/USDT', 6), spot_order('ETH/USDT', 6)]}, 'over-budget-operation')
    assert result['status'] == 'failed'
    assert '100' in result['error']
    with store.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM spot_budget_reservations').fetchone()[0] == 0


def test_narrowing_and_import_cannot_orphan_unrestricted_orders(unrestricted):
    database.save_order_log('owned', 'ETH/BTC', 'test', 'buy', 1, 0, 0, '',
                            trade_mode='SPOT_DCA', config_id='mcp:spot', amount=1, status='CANCELLED')
    profile = store.get('profile', 'spot')
    selected = {**profile, 'symbol_scope': 'selected', 'symbols': ['BTC/USDT']}
    with pytest.raises(ValueError, match='outside the selected'):
        cfg.save_profile(cfg.MCPProfile.model_validate(selected))
    exported = export_settings(False)
    exported['profiles'] = [selected if row['profile_id'] == 'spot' else row for row in exported['profiles']]
    with pytest.raises(ValueError, match='outside the selected'):
        import_settings(exported)
    assert store.get('profile', 'spot')['symbol_scope'] == 'all'
    database.upsert_spot_order_fill('owned', 'mcp:spot', 'ETH/BTC', 'CANCELLED', 0, 0, 0)
    cfg.save_profile(cfg.MCPProfile.model_validate(selected))
    assert store.get('profile', 'spot')['symbol_scope'] == 'selected'


def test_disabled_unrestricted_profiles_keep_all_owned_perpetual_symbols_for_maintenance(unrestricted, monkeypatch):
    from backend.app.core import scheduler
    from backend.utils import execution_ledger, execution_metrics, execution_stream

    with database.get_db_conn() as conn:
        for symbol in ('ETH/USDT:USDT', 'SOL/USDC:USDC'):
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(?,?,?,?)',
                         ('mcp:swap', symbol, 'LONG', json.dumps({'state': 'ACTIVE'})))
        conn.commit()
    profile = store.get('profile', 'swap')
    cfg.save_profile(cfg.MCPProfile.model_validate({**profile, 'enabled': False}))
    runtime = cfg.maintenance_profiles()[0]
    assert runtime['config_id'] == 'mcp:swap'
    assert runtime['enabled'] is False
    assert runtime['mcp_owned_symbols'] == ['ETH/USDT:USDT', 'SOL/USDC:USDC']
    seen = []

    class Ledger:
        def __init__(self, exchange, config_id, symbol):
            self.scope, self.symbol = config_id, symbol

        def sync(self):
            seen.append(self.symbol)
            return {}

    monkeypatch.setattr(scheduler, 'MarketTool', lambda **kwargs: SimpleNamespace(exchange=object()))
    monkeypatch.setattr(execution_ledger, 'ExecutionLedger', Ledger)
    monkeypatch.setattr(execution_stream, 'ensure_stream', lambda _: None)
    monkeypatch.setattr(execution_metrics, 'update_post_exit', lambda *args: None)
    scheduler._sync_execution(runtime)
    assert seen == runtime['mcp_owned_symbols']


def test_unrestricted_import_export_and_read_cancel_scopes_remain_enforced(unrestricted, monkeypatch):
    from backend.agent import tool_registry
    caller, _ = unrestricted
    export = export_settings(False)
    import_settings(export)
    assert {profile['symbol_scope'] for profile in cfg.profiles()} == {'all'}
    monkeypatch.setattr(tool_registry, 'run_trade_tool', lambda *a, **k: pytest.fail('permission gate must run first'))
    denied = service.execute({**caller, 'scopes': ['read']}, 'swap', 'ETH/USDT', 'cancel_orders_real',
                             {'order_id': 'foreign', 'reason': 'test'}, 'denied-operation')
    assert denied['status'] == 'failed' and 'scope' in denied['error']
    crossed = service.execute({**caller, 'profile_ids': ['spot']}, 'swap', 'ETH/USDT', 'cancel_orders_real',
                              {'order_id': 'foreign', 'reason': 'test'}, 'foreign-profile-operation')
    assert crossed['status'] == 'failed' and 'authorized' in crossed['error']


def test_real_spot_dispatch_uses_dynamic_symbols_and_preserves_cancel_ownership(unrestricted, monkeypatch):
    from backend.agent import agent_tools
    from backend.config import config

    caller, _ = unrestricted
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
    bought = service.execute(caller, 'spot', 'BTC/USDT', 'open_position_spot_dca',
                             {'orders': [spot_order(), spot_order('ETH/USDT')]}, 'real-dispatch-buy')
    assert bought['status'] == 'submitted', bought
    assert writes == [('BTC/USDT', 'BUY_LIMIT'), ('ETH/USDT', 'BUY_LIMIT')]
    foreign = service.execute(caller, 'spot', 'BTC/USDT', 'cancel_orders_real',
                              {'order_id': 'foreign-order', 'reason': 'test'}, 'real-dispatch-foreign')
    assert foreign['status'] == 'failed'
    assert len(writes) == 2
    # The selected portfolio path must also receive the gateway symbol explicitly.
    profile = store.get('profile', 'spot')
    cfg.save_profile(cfg.MCPProfile.model_validate({**profile, 'symbol_scope': 'selected',
                                                   'symbols': ['BTC/USDT', 'ETH/USDT']}))
    cancelled = service.execute(caller, 'spot', 'ETH/USDT', 'cancel_orders_real',
                                {'order_id': 'exchange-2', 'reason': 'test'}, 'selected-portfolio-cancel')
    assert cancelled['status'] == 'completed', cancelled
    assert writes[-1] == ('ETH/USDT', 'CANCEL')
    # The submitted budget reservation remains as durable evidence after a
    # cancellation. A verified zero-fill cancellation must not pin its symbol.
    with database.get_db_conn() as conn:
        reservation = conn.execute('SELECT status FROM spot_budget_reservations WHERE order_id=?',
                                   ('exchange-2',)).fetchone()
    assert reservation['status'] == 'submitted'
    cfg.save_profile(cfg.MCPProfile.model_validate(profile))  # Return to all.
    cfg.save_profile(cfg.MCPProfile.model_validate({**profile, 'symbol_scope': 'selected', 'symbols': ['BTC/USDT']}))
    assert store.get('profile', 'spot')['symbols'] == ['BTC/USDT']


def test_spot_maintenance_runs_all_quote_buckets_even_for_paused_profiles(unrestricted, monkeypatch):
    from backend.app.core import scheduler
    from backend.app.services import dashboard_service

    owned = [f'COIN{index}/USDT' for index in range(12)] + ['ETH/BTC', 'SOL/USDC']
    for index, symbol in enumerate(owned):
        database.save_order_log(str(index), symbol, 'test', 'buy', 1, 0, 0, '',
                                trade_mode='SPOT_DCA', config_id='mcp:spot', amount=1)
    profile = store.get('profile', 'spot')
    cfg.save_profile(cfg.MCPProfile.model_validate({**profile, 'enabled': False}))
    runtime = cfg.maintenance_profiles()[0]
    seen = []
    monkeypatch.setattr(dashboard_service, 'calculate_dca_stats',
                        lambda config_id: seen.append(list(cfg.get_runtime_profile(config_id)['symbols'])))
    scheduler.run_config_maintenance(runtime)
    assert sorted(symbol for batch in seen for symbol in batch) == sorted(owned)
    assert all(len(batch) <= 10 and len({symbol.split('/')[1] for symbol in batch}) == 1 for batch in seen)
    assert len(seen) == 4
    # The protection monitor dispatches paused MCP spot maintenance, outside of
    # the agent decision scheduler, and throttles repeat ticks.
    monkeypatch.setattr(scheduler, '_mcp_spot_maintenance_at', {})
    monkeypatch.setattr(scheduler, '_protection_executor', object())
    monkeypatch.setattr(scheduler, '_drop_finished_futures', lambda *args: None)
    monkeypatch.setattr(scheduler.global_config, 'get_all_symbol_configs', lambda: [])
    from backend.utils import execution_stream
    monkeypatch.setattr(execution_stream, 'stop_unused_streams', lambda: None)
    submitted = []
    monkeypatch.setattr(scheduler, '_submit_maintenance', lambda config: submitted.append(config['config_id']) or True)
    scheduler.protection_tick()
    scheduler.protection_tick()
    assert submitted == ['mcp:spot']


def test_linear_catalog_filter_applies_before_counts_pagination_and_selection(monkeypatch):
    from backend.app.services import market_catalog_service

    catalog = [
        {'symbol': symbol, 'base': 'BTC', 'quote': quote, 'linear': linear}
        for symbol, quote, linear in [('BTC/USD:BTC', 'USD', False), ('BTC/USDT:USDT', 'USDT', True)]
    ]
    monkeypatch.setattr(market_catalog_service, 'get_market_catalog', lambda *args: (catalog, False))
    filtered = market_catalog_service.list_market_symbols_payload(
        exchange='okx', market_type='swap', linear_only=True, limit=1,
        symbols=['BTC/USD:BTC', 'BTC/USDT:USDT'],
    )
    assert [item['symbol'] for item in filtered['symbols']] == ['BTC/USDT:USDT']
    assert filtered['total'] == 1 and filtered['has_more'] is False
    assert filtered['quote_currencies'] == ['USDT']
    assert filtered['invalid_symbols'] == ['BTC/USD:BTC']
    assert [item['symbol'] for item in filtered['selected_symbols']] == ['BTC/USDT:USDT']
    default = market_catalog_service.list_market_symbols_payload(exchange='okx', market_type='swap')
    assert default['total'] == 2
