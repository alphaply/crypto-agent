"""Monitor-only mode removes resource gates while retaining shared state safety."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import gc
import io
from backend import storage
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
import weakref

import pytest

from backend import database
from backend.mcp import exchange_runtime, store
from backend.mcp.resilience import Budget, current_budget
from backend.utils import news_context, public_catalogue, runtime_capacity


@pytest.fixture
def monitor_runtime(monkeypatch):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'false')
    monkeypatch.setenv('MCP_STORAGE_CONCURRENCY', '1')
    monkeypatch.setenv('MCP_DB_WAIT_SECONDS', '.01')
    for setting in ('RUNTIME_COORDINATOR_ENTRIES', 'RUNTIME_QUOTE_ENTRIES',
                    'RUNTIME_QUOTE_BYTES', 'RUNTIME_QUOTE_REFRESH_BYTES',
                    'RUNTIME_NEWS_CACHE_ENTRIES', 'RUNTIME_NEWS_CACHE_BYTES'):
        monkeypatch.setenv(setting, '1')
    manager = runtime_capacity.CapacityManager(byte_limit=1, limits_enabled=False)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    for name in ('_accounts', '_quotes', '_quote_refreshes'):
        monkeypatch.setattr(exchange_runtime, name, OrderedDict())
    monkeypatch.setattr(exchange_runtime, 'install_bounded_http', lambda _: None)
    monkeypatch.setattr(public_catalogue, '_catalogues', OrderedDict())
    monkeypatch.setattr(public_catalogue, '_consumer_leases', weakref.WeakKeyDictionary())
    monkeypatch.setattr(public_catalogue, '_client_slots', weakref.WeakKeyDictionary())
    for name in ('_memory_cache', '_failure_cache', '_digest_cache', '_source_locks'):
        monkeypatch.setattr(news_context, name, {})
    monkeypatch.setattr(store, '_storage_gate', None)
    monkeypatch.setattr(store, '_storage_capacity', None)
    monkeypatch.setattr(store, '_storage_active', 0)
    monkeypatch.setattr(store, '_storage_queued', 0)
    yield manager
    for name in ('_memory_cache', '_failure_cache', '_digest_cache'):
        getattr(news_context, name).clear()
    gc.collect()
    exchange_runtime.retire_runtime_caches(all_idle=True)


class Transport:
    timeout, rateLimit = 1000, 20

    def fetch(self, *args, **kwargs):
        return {'ok': True}


def test_monitor_mode_retains_shared_account_locks_and_counts_above_entry_cap(monitor_runtime):
    first, same_account, other_account = Transport(), Transport(), Transport()
    clients = [first, same_account, other_account]
    try:
        for client, key in zip(clients, ['shared', 'shared', 'other']):
            exchange_runtime.install_transport(client, key)
        shared = first._runtime_account
        assert shared is same_account._runtime_account
        assert shared is not other_account._runtime_account
        assert shared.references == 2
        assert exchange_runtime.diagnostics()['coordinators'] == 2
        assert monitor_runtime.snapshot()['byte_classes']['coordinator'] == 8192
        shared.network.acquire()
        token = current_budget.set(Budget(time.monotonic() + .02, 'monitor-lock', 'read'))
        try:
            with pytest.raises(exchange_runtime.BudgetExceeded, match='account_network'):
                same_account.fetch('https://fixture.invalid')
            assert shared.users == 0
        finally:
            current_budget.reset(token)
            shared.network.release()
        assert same_account.fetch('https://fixture.invalid') == {'ok': True}
        first.throttle(2)
        assert shared.next_at > time.monotonic()
        before = time.monotonic()
        same_account.throttle()
        assert time.monotonic() - before >= .025
    finally:
        for client in clients:
            exchange_runtime.close_runtime_client(client)
    exchange_runtime.retire_runtime_caches(all_idle=True)
    assert monitor_runtime.snapshot()['bytes'] == 0


def test_monitor_mode_quotes_ignore_count_bytes_and_refresh_caps_but_keep_ttl(monitor_runtime):
    calls = []
    def load():
        calls.append('load')
        return {'prices': [1, 2, 3], 'payload': 'x' * 1024}
    first, observed, cached = exchange_runtime.observe('first', 'ticker', load, 30)
    assert cached is False
    first['prices'].append(4)
    reused, same_time, cached = exchange_runtime.observe('first', 'ticker', load, 30)
    assert cached and same_time == observed and reused['prices'] == [1, 2, 3]
    exchange_runtime.observe('second', 'ticker', load, 30)
    assert len(exchange_runtime._quotes) == len(exchange_runtime._quote_refreshes) == 2
    assert exchange_runtime.diagnostics()['quote_bytes'] > 1024
    assert monitor_runtime.snapshot()['byte_classes']['parse'] == 0
    assert len(calls) == 2
    exchange_runtime._quotes[('first', 'ticker')]['expires_at'] = 0
    assert exchange_runtime.observe('first', 'ticker', load, 30)[2] is False
    assert len(calls) == 3
    exchange_runtime.retire_runtime_caches(all_idle=True)
    assert monitor_runtime.snapshot()['bytes'] == 0


def test_monitor_mode_quotes_still_single_flight(monitor_runtime):
    calls = []
    def load():
        calls.append('load')
        time.sleep(.02)
        return {'last': 7}
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: exchange_runtime.observe('shared', 'ticker', load, 30), range(3)))
    assert calls == ['load']
    assert len({item[1] for item in results}) == 1
    assert all(refresh['users'] == 0 and not refresh['lock'].locked()
               for refresh in exchange_runtime._quote_refreshes.values())


def test_enabled_mode_keeps_coordinator_and_quote_resource_gates(monitor_runtime, monkeypatch):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'true')
    monkeypatch.setattr(runtime_capacity, '_manager', runtime_capacity.CapacityManager(byte_limit=1024 * 1024))
    first, other = Transport(), Transport()
    try:
        exchange_runtime.install_transport(first, 'first')
        exchange_runtime.install_transport(other, 'other')
        monkeypatch.setenv('RUNTIME_QUOTE_BYTES', '65536')
        assert exchange_runtime.observe('quote', 'ticker', lambda: {'last': 7}, 30)[0] == {'last': 7}
        assert exchange_runtime._quotes
    finally:
        exchange_runtime.close_runtime_client(first)
        exchange_runtime.close_runtime_client(other)


def test_monitor_mode_news_source_locks_and_caches_survive_tiny_caps(monitor_runtime):
    with ExitStack() as locks:
        locks.enter_context(news_context._source_lock('first'))
        locks.enter_context(news_context._source_lock('second'))
        assert len(news_context._source_locks) == 2
        assert all(entry[0].locked() for entry in news_context._source_locks.values())
    assert news_context._source_locks == {}
    for key in ('first', 'second'):
        news_context._cache_put(news_context._memory_cache, key, {'items': ['x' * 1024]})
    assert set(news_context._memory_cache) == {'first', 'second'}
    assert monitor_runtime.snapshot()['byte_classes']['news'] > 2048
    news_context._memory_cache.clear()
    assert monitor_runtime.snapshot()['byte_classes']['news'] == 0


def test_enabled_mode_keeps_news_coordinator_and_cache_limits(monitor_runtime, monkeypatch):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'true')
    with news_context._source_lock('first'):
        with news_context._source_lock('second'):
            assert len(news_context._source_locks) == 2
    news_context._cache_put(news_context._memory_cache, 'oversized', {'items': ['x' * 1024]})
    assert news_context._memory_cache['oversized']


def test_market_symbol_catalogue_honors_monitor_mode_for_byte_limits(monitor_runtime, monkeypatch):
    from backend.app.services import market_catalog_service as service
    monkeypatch.setattr(service, '_market_symbol_cache', service._CatalogueCache())
    market = {'symbol': 'ETH/USDT', 'base': 'ETH', 'quote': 'USDT', 'active': True, 'spot': True}
    client = SimpleNamespace(markets={'ETH/USDT': market}, session=None)
    monkeypatch.setattr(service, '_public_exchange', lambda *_: client)
    monkeypatch.setattr(service, 'load_public_catalogue', lambda *_, **__: None)
    monkeypatch.setattr(service, 'release_catalogue_client', lambda *_: None)
    try:
        catalog, cached = service.get_market_catalog('binance', 'spot')
        assert not cached and [item['symbol'] for item in catalog] == ['ETH/USDT']
        assert monitor_runtime.snapshot()['byte_classes']['quotes'] > 1
        assert service.get_market_catalog('binance', 'spot')[1] is True
        service._market_symbol_cache.clear()
        monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'true')
        catalog, cached = service.get_market_catalog('binance', 'spot')
        assert not cached and [item['symbol'] for item in catalog] == ['ETH/USDT']
    finally:
        service._market_symbol_cache.clear()


def test_monitor_storage_tracks_workers_above_cap_until_they_finish(monitor_runtime):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    def worker(index):
        token = current_budget.set(Budget(time.monotonic() + 2, f'monitor-storage-{index}', 'read'))
        try:
            with store.storage_slot():
                entered[index].set()
                assert release.wait(timeout=1)
        finally:
            current_budget.reset(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, index) for index in range(2)]
        try:
            assert all(event.wait(timeout=1) for event in entered)
            assert store.storage_diagnostics() == {'active': 2, 'queued': 0, 'capacity': 1}
            assert not futures[0].cancel()
            assert store.storage_diagnostics()['active'] == 2
        finally:
            release.set()
        for future in futures:
            future.result()
    assert store.storage_diagnostics()['active'] == 0


def test_storage_monitor_mode_keeps_deadlines_and_enabled_mode_keeps_queue_gate(monitor_runtime, monkeypatch):
    expired = current_budget.set(Budget(time.monotonic() - 1, 'expired-storage', 'read'))
    try:
        with pytest.raises(exchange_runtime.BudgetExceeded, match='storage_queue'):
            with store.storage_slot():
                pytest.fail('Expired work must not start storage access')
        assert store.storage_diagnostics()['active'] == 0
    finally:
        current_budget.reset(expired)
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'true')
    token = current_budget.set(Budget(time.monotonic() + 1, 'limited-storage', 'read'))
    try:
        with store.storage_slot():
            with pytest.raises(ValueError, match='storage busy: queue timeout'):
                with store.storage_slot():
                    pytest.fail('Enabled admission must preserve the configured capacity')
            assert store.storage_diagnostics() == {'active': 1, 'queued': 0, 'capacity': 1}
        assert store.storage_diagnostics()['active'] == 0
    finally:
        current_budget.reset(token)


def test_monitor_storage_keeps_transactions_and_real_pg_lock_timeout(monitor_runtime, monkeypatch, tmp_path, pg):
    monkeypatch.setenv('MCP_DB_WAIT_SECONDS', '0.05')
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'monitor-storage.db'))
    with database.get_db_conn() as conn:
        conn.execute('CREATE TABLE example(value TEXT)')
        conn.commit()
    token = current_budget.set(Budget(time.monotonic() + 2, 'storage-transaction', 'read'))
    try:
        with store.storage_slot(), store.connection() as conn:
            assert store.storage_diagnostics()['active'] == 2
            timeout_ms = conn.execute('SHOW lock_timeout').fetchone()[0]
            assert timeout_ms.endswith('ms') and 0 < int(timeout_ms[:-2]) <= 50
            conn.execute("INSERT INTO example VALUES('rollback')")
            conn.rollback()
            assert conn.execute('SELECT count(*) FROM example').fetchone()[0] == 0
        with database.get_db_conn() as locked:
            locked.execute('LOCK TABLE example IN ACCESS EXCLUSIVE MODE')
            with pytest.raises(storage.OperationalError, match='(?:lock|statement) timeout'):
                with store.connection() as conn:
                    conn.execute('SELECT * FROM example').fetchall()
            assert store.storage_diagnostics()['active'] == 0
            locked.rollback()
    finally:
        current_budget.reset(token)


@pytest.mark.parametrize('redirect', [False, True])
def test_news_http_response_bound_is_disabled_only_in_monitor_mode(monitor_runtime, monkeypatch, redirect):
    payload = b'x' * (8 * 1024 * 1024 + 1)
    def install_response():
        calls = []
        def open_response(*args, **kwargs):
            calls.append(args)
            if redirect and len(calls) == 1:
                raise HTTPError('https://fixture.invalid/news', 302, 'redirect', {'Location': '/new'}, None)
            return io.BytesIO(payload)
        monkeypatch.setattr(news_context.urllib.request, 'urlopen', open_response)
    install_response()
    assert news_context._read_url('https://fixture.invalid/news') == payload
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'true')
    install_response()
    assert news_context._read_url('https://fixture.invalid/news') == payload
