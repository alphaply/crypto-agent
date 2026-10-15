import asyncio
import threading
import time

import pytest

from backend.mcp import resilience
from backend.utils import runtime_capacity


@pytest.fixture
def capacity(monkeypatch):
    manager = runtime_capacity.CapacityManager(total=3, reserved=1, queue_limit=2, wait_seconds=.02)
    monkeypatch.setattr(runtime_capacity, '_manager', manager)
    return manager


def test_request_deadline_keeps_physical_worker_commitment_and_blocks_replacement(capacity, monkeypatch):
    monkeypatch.setenv('MCP_READ_BUDGET_SECONDS', '.03')
    pool = resilience.WorkerPool('read', 1, 0)
    finish, entered = threading.Event(), threading.Event()
    calls = []
    def dependency():
        calls.append('original')
        entered.set()
        finish.wait(1)
        return {'result': 'original'}
    async def exercise():
        try:
            with pytest.raises(resilience.BudgetExceeded, match='retains capacity'):
                await pool.run(dependency)
            assert entered.is_set() and capacity.snapshot()['running'] == 1
            with pytest.raises(ValueError, match='busy'):
                await pool.run(lambda: calls.append('replacement'))
            assert calls == ['original'] and pool.snapshot()['active'] == 1
        finally:
            finish.set()
            while pool.snapshot()['active']:
                await asyncio.sleep(.001)
            pool.executor.shutdown()
        assert capacity.snapshot()['active'] == capacity.snapshot()['running'] == 0
    asyncio.run(exercise())


def test_full_aggregate_budget_cannot_queue_beyond_local_pool_limit(capacity, monkeypatch):
    monkeypatch.setenv('MCP_QUEUE_SECONDS', '.1')
    first, second = capacity.acquire('agent'), capacity.acquire('news')
    pool = resilience.WorkerPool('read', 4, 1)
    async def exercise():
        queued = asyncio.create_task(pool.run(lambda: 'later'))
        try:
            while pool.snapshot()['queued'] != 1:
                await asyncio.sleep(.001)
            with pytest.raises(ValueError, match='busy'):
                await pool.run(lambda: pytest.fail('overflow ran'))
            assert pool.snapshot()['queued'] == 1
            assert capacity.snapshot()['queued'] <= capacity.queue_limit
        finally:
            first.release()
            second.release()
        assert await queued == 'later'
        pool.executor.shutdown()
    asyncio.run(exercise())


def test_parent_round_deadline_bounds_wait_without_releasing_its_running_worker(capacity):
    pool = resilience.WorkerPool('read', 1, 0)
    finish = threading.Event()
    async def exercise():
        started = time.monotonic()
        try:
            with pytest.raises(resilience.BudgetExceeded):
                await pool.run(lambda: finish.wait(1), deadline=started + .02)
            assert time.monotonic() - started < .3
            assert pool.snapshot()['active'] == 1
        finally:
            finish.set()
            while pool.snapshot()['active']:
                await asyncio.sleep(.001)
            pool.executor.shutdown()
    asyncio.run(exercise())


def test_paper_recovery_uses_reserved_capacity_while_ordinary_read_pool_is_full(capacity):
    from backend.mcp.paper_storage import payload_scope
    reads = resilience.WorkerPool('read', 2, 0)
    recovery = resilience.WorkerPool('recovery', 1, 0)
    finish = threading.Event()
    recovered = []
    def recover_original():
        with payload_scope(critical=True):
            lease = runtime_capacity.current_capacity.get()
            assert lease.critical and lease.role == 'recovery'
            assert capacity.snapshot()['active'] == 3
            recovered.append('original-recovery-callback')
            return {'processed': 1}
    async def exercise():
        active = [asyncio.create_task(reads.run(lambda: finish.wait(2))) for _ in range(2)]
        try:
            while reads.snapshot()['active'] < 2:
                await asyncio.sleep(.001)
            with pytest.raises(ValueError, match='busy'):
                await reads.run(lambda: pytest.fail('ordinary overflow ran'))
            result = await recovery.run(recover_original)
            assert result == {'processed': 1} and recovered == ['original-recovery-callback']
            assert capacity.snapshot()['peaks']['active'] == capacity.total
        finally:
            finish.set()
            await asyncio.gather(*active)
            reads.executor.shutdown()
            recovery.executor.shutdown()
    asyncio.run(exercise())


@pytest.mark.parametrize('name,value', [('READ_CONCURRENCY', '0'), ('READ_QUEUE_LIMIT', '1.1'),
                                        ('READ_BUDGET_SECONDS', 'nan'), ('QUEUE_SECONDS', 'bad')])
def test_invalid_mcp_capacity_configuration_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv('MCP_' + name, value)
    with pytest.raises(ValueError, match='MCP_'):
        resilience.setting(name, 2, minimum=0 if 'QUEUE_LIMIT' in name else .001)
