import asyncio
import logging
from backend import storage
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import database, storage
from backend.mcp import auth, service, store
from backend.mcp import resilience as runtime
from backend.mcp import exchange_runtime as exchange_runtime


def _capacity_mode(monkeypatch, *, enabled):
    from backend.utils import runtime_capacity

    manager = runtime_capacity.CapacityManager(total=2, reserved=1, queue_limit=0,
                                               limits_enabled=enabled)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    return manager


@pytest.fixture
def db(tmp_path, monkeypatch, financial_pg):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'mcp.db'))
    with database.get_db_conn() as conn:
        from backend.database.schema import initialize_schema
        pass  # PostgreSQL migrations are installed by financial_pg.
        store.initialize_schema(conn)
        conn.commit()
    return database.DB_NAME


def test_occupied_read_workers_do_not_delay_web_or_auth(monkeypatch):
    pool = runtime.WorkerPool('read', 2, 1)
    release = threading.Event()
    app = FastAPI()

    @app.get('/health')
    async def health():
        return {'healthy': True}

    @app.get('/web')
    def web():
        return {'web': True}

    async def exercise():
        workers = [asyncio.create_task(pool.run(lambda: release.wait(2))) for _ in range(2)]
        while pool.snapshot()['active'] < 2:
            await asyncio.sleep(.001)
        queued = asyncio.create_task(pool.run(lambda: 'later'))
        while pool.snapshot()['queued'] < 1:
            await asyncio.sleep(.001)
        with pytest.raises(ValueError, match='busy'):
            await pool.run(lambda: 'overflow')
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                start = time.monotonic()
                assert (await client.get('/health')).status_code == 200
                assert (await client.get('/web')).status_code == 200
                assert await runtime.run('auth', lambda: 'authenticated') == 'authenticated'
                assert time.monotonic() - start < .5
        finally:
            release.set()
            await asyncio.gather(*workers, queued)
            pool.executor.shutdown()
        assert pool.snapshot()['active'] == pool.snapshot()['queued'] == 0
    asyncio.run(exercise())


def test_disconnect_does_not_release_running_worker_or_repeat_submission(monkeypatch):
    pool = runtime.WorkerPool('execute', 1, 0)
    release = threading.Event()
    entered = threading.Event()
    submissions = []

    def write():
        submissions.append('original')
        entered.set()
        release.wait(2)
        return 'ack'

    async def exercise():
        task = asyncio.create_task(pool.run(write))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.snapshot()['active'] == 1
        with pytest.raises(ValueError, match='busy'):
            await pool.run(lambda: submissions.append('duplicate'))
        release.set()
        while pool.snapshot()['active']:
            await asyncio.sleep(.001)
        assert submissions == ['original']
        pool.executor.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['read', 'execute', 'auth', 'recovery'])
def test_monitor_only_pool_starts_all_workers_without_capacity_or_queue_gate(monkeypatch, kind):
    manager = _capacity_mode(monkeypatch, enabled=False)
    manager.set_pressure(True, 'test-pressure')
    pool = runtime.WorkerPool(kind, 1, 0)
    release = threading.Event()
    entered = []
    lock = threading.Lock()

    def work(index):
        budget = runtime.current_budget.get()
        lease = runtime.current_capacity.get()
        assert budget.kind == kind and lease.manager is manager
        with lock:
            entered.append((index, threading.current_thread().daemon))
        release.wait(2)
        return index

    async def exercise():
        tasks = [asyncio.create_task(pool.run(partial(work, index))) for index in range(5)]
        try:
            async def all_started():
                while len(entered) < 5:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(all_started(), 1)
            snapshot = pool.snapshot()
            assert snapshot['active'] == 5 and snapshot['queued'] == 0
            assert snapshot['limits_enabled'] is False
            assert all(not daemon for _, daemon in entered)
            assert manager.snapshot()['active'] == manager.snapshot()['running'] == 5
            release.set()
            assert await asyncio.gather(*tasks) == list(range(5))
            assert pool.snapshot()['active'] == manager.snapshot()['active'] == 0
            assert manager.snapshot()['running'] == 0
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            pool.executor.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('ending', ['cancel', 'deadline'])
def test_monitor_only_disconnected_or_expired_request_keeps_physical_write_owned(monkeypatch, ending):
    manager = _capacity_mode(monkeypatch, enabled=False)
    if ending == 'deadline':
        monkeypatch.setenv('MCP_EXECUTE_BUDGET_SECONDS', '.03')
    pool = runtime.WorkerPool('execute', 1, 0)
    release, entered = threading.Event(), threading.Event()
    submissions = []

    def write():
        submissions.append('original')
        entered.set()
        release.wait(2)
        return 'ack'

    async def exercise():
        task = asyncio.create_task(pool.run(write))
        try:
            async def started():
                while not entered.is_set():
                    await asyncio.sleep(.001)
            await asyncio.wait_for(started(), 1)
            if ending == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(runtime.BudgetExceeded, match='retains capacity'):
                    await task
            assert pool.snapshot()['active'] == manager.snapshot()['active'] == 1
            assert manager.snapshot()['running'] == 1
            assert submissions == ['original']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            async def finished():
                while pool.snapshot()['active']:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(finished(), 1)
            pool.executor.shutdown()
        assert manager.snapshot()['active'] == manager.snapshot()['running'] == 0
        assert submissions == ['original']
    asyncio.run(exercise())


def test_monitor_only_thread_start_failure_releases_unstarted_ownership(monkeypatch):
    manager = _capacity_mode(monkeypatch, enabled=False)
    pool = runtime.WorkerPool('read', 1, 0)
    calls = []

    def fail_start(_thread):
        raise RuntimeError('thread start failed')

    monkeypatch.setattr(runtime.threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError, match='thread start failed'):
        asyncio.run(pool.run(lambda: calls.append('executed')))
    pool.executor.shutdown()
    assert not calls
    assert pool.snapshot()['active'] == pool.snapshot()['queued'] == 0
    assert manager.snapshot()['active'] == manager.snapshot()['running'] == 0


def test_queue_wait_is_in_total_budget_and_cancelled_queue_recovers(monkeypatch):
    monkeypatch.setenv('MCP_READ_BUDGET_SECONDS', '.05')
    monkeypatch.setenv('MCP_QUEUE_SECONDS', '.03')
    pool = runtime.WorkerPool('read', 1, 2)
    release = threading.Event()
    async def exercise():
        worker = asyncio.create_task(pool.run(lambda: release.wait(1)))
        while not pool.snapshot()['active']:
            await asyncio.sleep(.001)
        start = time.monotonic()
        with pytest.raises(ValueError, match='queue timeout'):
            await pool.run(lambda: None)
        assert time.monotonic() - start < .2
        queued = asyncio.create_task(pool.run(lambda: None))
        await asyncio.sleep(.005)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert pool.snapshot()['queued'] == 0
        release.set()
        await worker
        pool.executor.shutdown()
    asyncio.run(exercise())


def test_deadline_reaches_every_http_call_and_stops_later_io(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock[0])
    attempts = []
    exchange = SimpleNamespace(timeout=15000, rateLimit=1)
    def fetch(*args):
        attempts.append(exchange.timeout)
        clock[0] += .02
        return {'ok': True}
    exchange.fetch = fetch
    exchange_runtime.install_transport(exchange, 'test-deadline')
    budget = runtime.Budget(time.monotonic() + .05, 'deadline-test', 'read')
    token = runtime.current_budget.set(budget)
    try:
        exchange.fetch('https://example.test')
        exchange.fetch('https://example.test')
        clock[0] += .02
        with pytest.raises(runtime.BudgetExceeded):
            exchange.fetch('https://example.test')
    finally:
        runtime.current_budget.reset(token)
    assert len(attempts) == 2
    assert attempts[1] < attempts[0] <= 50
    assert exchange.timeout == 15000


def test_market_refresh_is_single_and_clients_do_not_share_mutable_metadata():
    calls = []
    class Exchange:
        markets = currencies = None
        def load_markets(self):
            calls.append('refresh')
            time.sleep(.02)
            self.markets, self.currencies = {'ETH': {'price': 5}}, {'USD': {'precision': 2}}
        def set_markets(self, markets, currencies):
            self.markets, self.currencies = markets, currencies
    clients = [Exchange() for _ in range(8)]
    key = exchange_runtime.account_key('binance', 'swap', ['key1', 'revision1'])
    with ThreadPoolExecutor(8) as executor:
        times = list(executor.map(lambda client: exchange_runtime.load_catalogue(client, key), clients))
    assert calls == ['refresh']
    assert len(set(times)) == 1
    clients[0].markets['ETH']['price'] = 99
    assert clients[1].markets['ETH']['price'] == 5
    other = Exchange()
    exchange_runtime.load_catalogue(other, exchange_runtime.account_key('binance', 'spot', ['key1', 'revision1']))
    exchange_runtime.load_catalogue(other, exchange_runtime.account_key('binance', 'swap', ['key1', 'revision2']))
    assert len(calls) == 3


def test_account_clients_share_weighted_rate_coordinator():
    a = SimpleNamespace(timeout=15000, rateLimit=25, fetch=lambda *args: 'ok')
    b = SimpleNamespace(timeout=15000, rateLimit=25, fetch=lambda *args: 'ok')
    exchange_runtime.install_transport(a, 'shared-account')
    exchange_runtime.install_transport(b, 'shared-account')
    a.throttle(2)
    start = time.monotonic()
    b.throttle(1)
    assert time.monotonic() - start >= .04


def test_migration_preserves_records_is_idempotent_and_reads_do_not_ddl(db, monkeypatch):
    store.put('settings', 'test', {'enabled': True})
    statements = []
    original = database.get_db_conn
    from contextlib import contextmanager
    class TracedConnection:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, statement, *args, **kwargs):
            statements.append(str(statement))
            return self.conn.execute(statement, *args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.conn, name)
    @contextmanager
    def traced(**kwargs):
        with original(**kwargs) as conn:
            yield TracedConnection(conn)
    monkeypatch.setattr(database, 'get_db_conn', traced)
    assert store.get('settings', 'test') == {'enabled': True}
    assert not any('CREATE' in statement.upper() for statement in statements)
    with database.get_db_conn() as conn:
        conn.execute('BEGIN')
        conn.execute("INSERT INTO mcp_records (kind,id,payload) VALUES('test','rollback','{}')")
        store.initialize_schema(conn)
        assert conn.info.transaction_status != 0
        conn.rollback()
    assert store.get('test', 'rollback') is None
    assert store.get('settings', 'test')['enabled']


def test_auth_lock_wait_is_bounded_and_does_not_block_event_loop(db, monkeypatch):
    monkeypatch.setenv('MCP_DB_WAIT_SECONDS', '.03')
    provider = auth.AuthorizationProvider('http://localhost:7860')
    conn = storage.connect()
    conn.execute('LOCK TABLE mcp_records IN ACCESS EXCLUSIVE MODE')
    async def exercise():
        task = asyncio.create_task(provider.get_client('not-existing'))
        start = time.monotonic()
        await asyncio.sleep(.005)
        assert time.monotonic() - start < .05
        with pytest.raises(storage.OperationalError, match='timeout'):
            await task
        assert time.monotonic() - start < .3
    try:
        asyncio.run(exercise())
    finally:
        conn.rollback()
        conn.close()


def test_market_partial_and_cache_preserve_observation_time(monkeypatch):
    profile = {'market_type': 'spot', 'symbols': ['ETH/USDT'], 'symbol_scope': 'selected'}
    monkeypatch.setattr(service, 'require', lambda *args: profile)
    monkeypatch.setattr(store, 'audit_safely', lambda *args, **kwargs: None)
    calls = []
    exchange = SimpleNamespace(
        fetch_ticker=lambda symbol: calls.append('ticker') or {'last': 5},
        fetch_ohlcv=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(service, 'market_tool', lambda *args: SimpleNamespace(exchange=exchange, mcp_account_key='partial-ticker'))
    caller = {'id': 'reader'}
    first = service.query(caller, 'market', 'test', 'ETH/USDT')
    second = service.query(caller, 'market', 'test', 'ETH/USDT')
    assert first['status'] == 'partial'
    assert first['candles'] is None and first['missing_sources'] == ['candles']
    assert second['observed_at']['ticker'] == first['observed_at']['ticker']
    assert second['sources']['ticker'] == 'public_cache'
    assert calls == ['ticker']


def test_missing_order_channel_never_returns_partial_as_complete(monkeypatch):
    calls = []
    def fetch(symbol, limit, params):
        calls.append(params)
        if params:
            raise TimeoutError('secret-signed-url')
        return []
    mt = SimpleNamespace(exchange=SimpleNamespace(fetch_open_orders=fetch))
    with pytest.raises(ValueError, match=r'missing_sources=\[algo\]') as exc:
        service._independent_orders_view(mt, 'ETH/USDT:USDT', 10)
    assert 'secret' not in str(exc.value)
    assert len(calls) == 2


def test_stage_logs_are_correlated_without_signed_url_or_arguments(caplog):
    pool = runtime.WorkerPool('read', 1, 1)
    def failure():
        with runtime.stage('exchange_http'):
            raise TimeoutError('https://secret.test?signature=super-secret')
    with caplog.at_level(logging.INFO, logger='MCPResilience'):
        with pytest.raises(TimeoutError):
            asyncio.run(pool.run(failure, label='balance'))
    pool.executor.shutdown()
    assert 'request_id=' in caplog.text and 'stage=exchange_http' in caplog.text
    assert 'super-secret' not in caplog.text and 'secret.test' not in caplog.text


def _financial_gateway(monkeypatch):
    from contextlib import nullcontext
    profile = {'market_type': 'swap', 'symbols': ['ETH/USDT:USDT'], 'symbol_scope': 'selected'}
    monkeypatch.setattr(service, 'require', lambda *args: profile)
    monkeypatch.setattr(service, 'trading_tools', lambda *args: [{'name': 'cancel'}])
    monkeypatch.setattr(service, 'runtime_symbols', lambda *args: nullcontext())
    monkeypatch.setattr(service, '_recover_trade_receipt', lambda *args: None)
    monkeypatch.setattr(service, 'sanitize', lambda value, *args: value)
    caller = {'id': 'resilience-caller', 'scopes': ['read', 'cancel'], 'profile_ids': ['test']}
    args = ('test', 'ETH/USDT:USDT', 'cancel', {'order_id': 'regular:42', 'reason': 'test'}, 'stable-original-operation')
    return caller, args


@pytest.mark.parametrize('limits_enabled', [True, False])
def test_lost_ack_and_expired_budget_keep_original_identity_and_only_one_write(db, monkeypatch, limits_enabled):
    import backend.agent.tool_registry as registry
    _capacity_mode(monkeypatch, enabled=limits_enabled)
    caller, args = _financial_gateway(monkeypatch)
    submissions = []
    def lost_ack(*args, **kwargs):
        submissions.append(kwargs['operation_id'])
        budget = runtime.current_budget.get()
        budget.write_started = True
        budget.deadline = time.monotonic() - 1
        raise runtime.BudgetExceeded('MCP budget exhausted at exchange_http_response')
    monkeypatch.setattr(registry, 'run_trade_tool', lost_ack)
    first = asyncio.run(runtime.run('execute', partial(service.execute, caller, *args)))
    assert first['status'] == 'unknown' and first['submission'] == 'possibly_sent'
    assert first['operation_id'] == args[-1]
    # The final durable write has a short storage grace, even with I/O expired.
    second = asyncio.run(runtime.run('execute', partial(service.execute, caller, *args)))
    assert first == second and len(submissions) == 1
    observed = asyncio.run(runtime.run('read', partial(service.operation, caller, 'test', args[-1])))
    assert observed['state'] == 'unknown' and observed['result']['operation_id'] == args[-1]


@pytest.mark.parametrize('limits_enabled', [True, False])
def test_expired_budget_after_confirmed_ack_preserves_ack_and_audit_failure(db, monkeypatch, limits_enabled):
    import backend.agent.tool_registry as registry
    _capacity_mode(monkeypatch, enabled=limits_enabled)
    caller, args = _financial_gateway(monkeypatch)
    def ack(*args, **kwargs):
        budget = runtime.current_budget.get()
        budget.write_started = True
        budget.deadline = time.monotonic() - 1
        return {'receipt_version': 1, 'status': 'submitted', 'order_id': 'regular:42'}
    monkeypatch.setattr(registry, 'run_trade_tool', ack)
    monkeypatch.setattr(store, 'audit', lambda *args, **kwargs: (_ for _ in ()).throw(storage.OperationalError('locked')))
    result = asyncio.run(runtime.run('execute', partial(service.execute, caller, *args)))
    assert result['status'] == 'submitted' and result['result']['order_id'] == 'regular:42'
    assert service.operation(caller, 'test', args[-1])['state'] == 'submitted'


@pytest.mark.parametrize('limits_enabled', [True, False])
def test_budget_exhausted_before_dispatch_is_explicitly_not_submitted(db, monkeypatch, limits_enabled):
    import backend.agent.tool_registry as registry
    _capacity_mode(monkeypatch, enabled=limits_enabled)
    caller, args = _financial_gateway(monkeypatch)
    calls = []
    monkeypatch.setattr(registry, 'run_trade_tool', lambda *args, **kwargs: calls.append(args))
    original = store.reserve_operation
    def slow_reserve(*args):
        value = original(*args)
        runtime.current_budget.get().deadline = time.monotonic() - 1
        return value
    monkeypatch.setattr(store, 'reserve_operation', slow_reserve)
    result = asyncio.run(runtime.run('execute', partial(service.execute, caller, *args)))
    assert result['status'] == 'failed' and result['submission'] == 'not_submitted'
    assert not calls


def test_startup_uses_previously_migrated_mcp_tables_before_reading_settings(tmp_path, monkeypatch):
    from backend.mcp.server import install_mcp
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'fresh.db'))
    app = FastAPI()
    install_mcp(app)
    assert store.get('settings', 'global') is None
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
    assert {'mcp_operations', 'mcp_operation_requests', 'mcp_operation_revisions', 'mcp_records'} <= tables


def test_failed_market_refresh_is_backed_off_and_never_serves_stale():
    calls = []
    class Exchange:
        def load_markets(self):
            calls.append('refresh')
            raise TimeoutError('private-signature')
    key = 'failed-catalogue-refresh'
    with pytest.raises(TimeoutError):
        exchange_runtime.load_catalogue(Exchange(), key)
    with pytest.raises(ValueError, match='refresh backoff'):
        exchange_runtime.load_catalogue(Exchange(), key)
    assert calls == ['refresh']


def test_storage_has_independent_finite_capacity_and_reclaims_after_failure(db, monkeypatch):
    monkeypatch.setenv('MCP_DB_WAIT_SECONDS', '.01')
    capacity = store.storage_diagnostics()['capacity'] or 2
    holders = []
    tokens = []
    try:
        for index in range(capacity):
            token = runtime.current_budget.set(runtime.Budget(time.monotonic() + 1, f'storage-{index}', 'read'))
            slot = store.storage_slot()
            slot.__enter__()
            tokens.append(token)
            holders.append(slot)
        with pytest.raises(ValueError, match='storage busy'):
            with store.connection():
                pytest.fail('overflow may not open another DB connection')
        assert store.storage_diagnostics()['active'] == capacity
        assert store.storage_diagnostics()['queued'] == 0
    finally:
        for slot, token in reversed(list(zip(holders, tokens))):
            slot.__exit__(None, None, None)
            runtime.current_budget.reset(token)
    assert store.storage_diagnostics()['active'] == 0


@pytest.fixture(autouse=True)
def _postgres_storage(financial_pg):
    """Run original financial contracts against an isolated PostgreSQL schema."""
    yield
