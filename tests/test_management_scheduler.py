import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import database
from backend.app.core import scheduler
from backend.database_schema import initialize_schema
from backend.mcp import resilience
from backend.utils import position_management
from test_exclusive_position_management import _hold_reservation, ExclusiveExchange, make_service, SYMBOL


def test_failed_scope_does_not_starve_other_scope_or_recovery(monkeypatch, caplog):
    clock, calls = [0.0], []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(scheduler, '_exclusive_maintenance_retry', {})
    for name, phase in [('recover_reservations', 'recovery'), ('ensure_managed', 'discovery'),
                        ('cleanup_previous_orders', 'cleanup')]:
        def callback(manager, symbol, phase=phase):
            calls.append((symbol, phase))
            if symbol == 'BAD/USDT:USDT':
                raise RuntimeError('https://secret.test?signature=private')
        monkeypatch.setattr(position_management, name, callback)
    config = {'config_id': 'paused-owner', 'enabled': False, 'management_scopes': [
        {'symbol': 'BAD/USDT:USDT', 'side': 'LONG'}, {'symbol': SYMBOL, 'side': 'LONG'},
        {'symbol': SYMBOL, 'side': 'SHORT'}]}
    scheduler._run_exclusive_maintenance(object(), config)
    assert calls == [('BAD/USDT:USDT', 'recovery'), ('BAD/USDT:USDT', 'discovery'),
                     ('BAD/USDT:USDT', 'cleanup'), (SYMBOL, 'recovery'),
                     (SYMBOL, 'discovery'), (SYMBOL, 'cleanup')]
    calls.clear()
    scheduler._run_exclusive_maintenance(object(), config)
    assert calls == []
    clock[0] = 31
    scheduler._run_exclusive_maintenance(object(), config)
    calls.clear()
    clock[0] = 61
    scheduler._run_exclusive_maintenance(object(), config)
    assert calls == [(SYMBOL, 'recovery'), (SYMBOL, 'discovery'), (SYMBOL, 'cleanup')]
    assert 'signature=private' not in caplog.text


def test_minute_scheduler_and_monitor_merge_same_config_mutable_client_use(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(scheduler, '_maintenance_run_locks', {})
    def work(config):
        calls.append(config['config_id'])
        entered.set()
        release.wait(2)
    monkeypatch.setattr(scheduler, '_run_config_maintenance', work)
    with ThreadPoolExecutor(2) as executor:
        original = executor.submit(scheduler.run_config_maintenance, {'config_id': 'same-account-task'})
        assert entered.wait(1)
        overlapping = executor.submit(scheduler.run_config_maintenance, {'config_id': 'same-account-task'})
        assert overlapping.result(timeout=1) is None
        assert calls == ['same-account-task']
        release.set()
        original.result(timeout=1)


def test_scheduler_process_reservation_and_slow_mcp_keep_web_responsive(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'two-process.db'))
    monkeypatch.setenv('MCP_READ_BUDGET_SECONDS', '.06')
    monkeypatch.setenv('MCP_QUEUE_SECONDS', '.02')
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_hold_reservation, args=(database.DB_NAME, entered, release))
    pool = resilience.WorkerPool('read', 2, 1)
    app = FastAPI()
    @app.get('/health')
    def health():
        with database.get_db_conn() as conn:
            return {'healthy': conn.execute('SELECT 1').fetchone()[0] == 1}
    def slow_exchange():
        time.sleep(.08)
        return resilience.remaining('exchange_response')
    async def exercise():
        active = [asyncio.create_task(pool.run(slow_exchange)) for _ in range(2)]
        while pool.snapshot()['active'] < 2:
            await asyncio.sleep(.001)
        started = time.monotonic()
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://local') as client:
            assert (await client.get('/health')).json()['healthy']
        assert time.monotonic() - started < 2
        service = make_service(ExclusiveExchange())
        with pytest.raises(ValueError, match='running'):
            with position_management.action_reservation(service, SYMBOL, 'LONG', 'web-competing-write'):
                pytest.fail('Web worker bypassed scheduler financial scope')
        with pytest.raises(ValueError, match='queue timeout'):
            await pool.run(lambda: None)
        outcomes = await asyncio.gather(*active, return_exceptions=True)
        assert all(isinstance(outcome, resilience.BudgetExceeded) for outcome in outcomes)
        assert time.monotonic() - started < .5
        assert pool.snapshot()['active'] == pool.snapshot()['queued'] == 0
    process.start()
    try:
        assert entered.wait(10)
        asyncio.run(exercise())
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
        pool.executor.shutdown()
    assert process.exitcode == 0
