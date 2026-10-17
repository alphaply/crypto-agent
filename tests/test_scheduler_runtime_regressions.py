"""Production regressions: task modes, account discovery and concrete streams."""
import concurrent.futures
import json
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest


def test_non_real_management_profiles_do_not_become_orphan_live_plans(financial_pg, monkeypatch):
    from backend.app.core import scheduler
    from backend.database import get_db_conn
    from backend.mcp import settings
    from backend.utils import execution_stream

    configs = [
        {'config_id': mode, 'mode': mode, 'enabled': True,
         'position_management_policy': 'visible_first_claim'}
        for mode in ('STRATEGY', 'SPOT_DCA', 'REAL')
    ]
    spot_profile = {**configs[1], 'config_id': 'mcp:spot'}
    with get_db_conn() as conn:
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                     ('missing-owner', 'ETH/USDT:USDT', 'LONG', json.dumps({'state': 'ACTIVE'})))
        conn.commit()
    monkeypatch.setattr(scheduler.global_config, 'get_all_symbol_configs', lambda: configs)
    monkeypatch.setattr(settings, 'maintenance_profiles', lambda: [spot_profile])
    monkeypatch.setattr(scheduler, '_submit_execution_sync', lambda _: None)
    spot_jobs = []
    monkeypatch.setattr(scheduler, '_submit_maintenance', lambda cfg: spot_jobs.append(cfg['config_id']) or True)
    monkeypatch.setattr(scheduler, '_mcp_spot_maintenance_at', {})
    monkeypatch.setattr(scheduler, '_protection_futures', {})
    monkeypatch.setattr(scheduler, '_protection_executor', object())
    monkeypatch.setattr(scheduler, '_drop_finished_futures', lambda *_: None)
    monkeypatch.setattr(scheduler, '_retire_scheduler_clients', lambda _: None)
    monkeypatch.setattr(scheduler, '_tick_background_receipts', lambda: None)
    monkeypatch.setattr(execution_stream, 'stop_unused_streams', lambda: None)
    jobs = []

    def submit(executor, role, fn, cfg, **kwargs):
        jobs.append(cfg['config_id'])
        return concurrent.futures.Future()

    monkeypatch.setattr(scheduler, 'submit_capacity', submit)
    logger = Mock()
    monkeypatch.setattr(scheduler, 'logger', logger)
    scheduler.protection_tick()
    assert jobs == ['REAL']
    assert spot_jobs == ['mcp:spot']
    # A genuinely orphaned persisted plan must still be reported, not hidden.
    logger.error.assert_called_once()
    assert 'missing-owner' in logger.error.call_args.args[0]


@pytest.mark.parametrize('market_type', ['spot', 'swap'])
@pytest.mark.parametrize('scope', ['selected', 'all'])
def test_only_authorized_account_discovery_allows_binance_no_symbol_orders(monkeypatch, market_type, scope):
    from backend.config import config
    from backend.mcp import exchange_runtime
    from backend.utils.market_data import MarketTool

    cfg = {'exchange': 'binance', 'mode': 'SPOT_DCA' if market_type == 'spot' else 'REAL',
           'market_type': market_type, 'symbol': None, 'mcp_symbol_scope': scope,
           'position_management_policy': 'visible_first_claim'}
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: cfg)
    monkeypatch.setattr(config, 'get_binance_credentials', lambda **_: ('test-key', 'test-secret'))
    monkeypatch.setattr(exchange_runtime, 'install_transport', lambda *_: None)
    monkeypatch.setattr(exchange_runtime, 'load_catalogue', lambda *_, **__: 0)
    tool = MarketTool(config_id='mcp:test')
    calls = []
    tool.exchange.load_markets = lambda: {}
    tool.exchange.fetch2 = lambda *args, **kwargs: calls.append(args) or []
    if scope == 'selected':
        with pytest.raises(ccxt.ExchangeError, match='warnOnFetchOpenOrdersWithoutSymbol'):
            tool.exchange.fetch_open_orders()
        assert not calls
    else:
        assert tool.exchange.fetch_open_orders() == []
        assert len(calls) == 1
        assert calls[0][0] == 'openOrders'
        assert calls[0][1] == ('private' if market_type == 'spot' else 'fapiPrivate')
        assert 'symbol' not in calls[0][3]
    tool.close()


def test_all_symbol_streams_use_each_owned_market_and_no_empty_ledger(monkeypatch):
    import ccxt.pro
    from backend.utils import execution_stream as streams
    from backend.utils import runtime_capacity

    workers = {}
    monkeypatch.setattr(streams, '_workers', workers)
    monkeypatch.setattr(streams, 'account_scope', lambda *_: 'scope')
    monkeypatch.setattr(runtime_capacity, '_manager',
                        runtime_capacity.CapacityManager(total=6, reserved=1, byte_limit=128 * 1024 * 1024))
    ledgers, consumed, closed = [], [], []

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    class Client:
        def __init__(self, options):
            pass

        async def close(self):
            closed.append(True)

    class Ledger:
        def __init__(self, exchange, config_id, symbol):
            assert symbol is not None
            self.symbol = symbol
            ledgers.append(symbol)

    async def consume(client, ledger, stop):
        consumed.append(ledger.symbol)
        await client.close()

    monkeypatch.setattr(streams.threading, 'Thread', InlineThread)
    monkeypatch.setattr(ccxt.pro, 'binanceusdm', Client)
    monkeypatch.setattr(streams, 'ExecutionLedger', Ledger)
    monkeypatch.setattr(streams, 'consume', consume)
    tool = SimpleNamespace(runtime_config={'mode': 'REAL'}, market_type='swap', symbol=None,
                           config_id='mcp:all', exchange=SimpleNamespace(
                               id='binanceusdm', apiKey='test', secret='test', password='', options={}, timeout=500))
    assert streams.ensure_stream(tool) is False
    assert workers == {}
    symbols = ['ETH/USDT:USDT', 'SOL/USDC:USDC']
    for symbol in symbols:
        assert streams.ensure_stream(tool, symbol=symbol)
    assert ledgers == consumed == symbols
    assert len(closed) == 2
    assert set(workers) == {('scope', symbol) for symbol in symbols}
    assert runtime_capacity.get_capacity_manager().snapshot()['active'] == 0


def test_unrestricted_account_without_owned_symbols_does_not_start_stream(monkeypatch):
    from backend.app.core import scheduler
    from backend.utils import execution_stream

    monkeypatch.setattr(scheduler, 'MarketTool', lambda **_: SimpleNamespace())
    start = Mock()
    monkeypatch.setattr(execution_stream, 'ensure_stream', start)
    scheduler._sync_execution({'config_id': 'mcp:all', 'mode': 'REAL',
                               'mcp_symbol_scope': 'all', 'mcp_owned_symbols': []})
    start.assert_not_called()


def test_stream_rotation_waits_for_worker_and_ledger_failure_opens_no_client(monkeypatch):
    import ccxt.pro
    from backend.utils import execution_stream as streams
    from backend.utils import runtime_capacity

    manager = runtime_capacity.CapacityManager(total=3, reserved=1)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    monkeypatch.setattr(streams, '_workers', {})
    monkeypatch.setattr(streams, 'account_scope', lambda *_: 'scope')
    threads = []

    class DeferredThread:
        def __init__(self, target, **kwargs):
            self.target, self.live = target, False
            threads.append(self)

        def start(self):
            self.live = True

        def is_alive(self):
            return self.live

    client = Mock()
    monkeypatch.setattr(streams.threading, 'Thread', DeferredThread)
    monkeypatch.setattr(ccxt.pro, 'binanceusdm', client)
    monkeypatch.setattr(streams, 'ExecutionLedger', Mock(side_effect=RuntimeError('isolated ledger failure')))
    tool = SimpleNamespace(runtime_config={'mode': 'REAL'}, market_type='swap', symbol='ETH/USDT:USDT',
                           config_id='cfg', exchange=SimpleNamespace(
                               id='binanceusdm', apiKey='test', secret='old', password='', options={}, timeout=500))
    assert streams.ensure_stream(tool)
    retained = manager.snapshot()['bytes']
    assert manager.snapshot()['active'] == 1 and retained > 0
    tool.exchange.secret = 'rotated'
    assert streams.ensure_stream(tool) is False
    assert len(threads) == 1
    assert streams._workers[('scope', tool.symbol)]['stop'].is_set()
    assert manager.snapshot()['bytes'] == retained
    threads[0].target()
    client.assert_not_called()
    assert manager.snapshot()['active'] == manager.snapshot()['bytes'] == 0
