"""Resource commitments are independent of caller cancellation and pool layout."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from backend.utils import runtime_capacity as capacity


@pytest.fixture
def manager(monkeypatch):
    value = capacity.CapacityManager(total=3, reserved=1, queue_limit=2,
                                     wait_seconds=.05, byte_limit=100)
    monkeypatch.setattr(capacity, '_manager', value)
    return value


def test_cross_pool_total_and_reserved_capacity_are_one_budget(manager):
    first = manager.acquire('agent', timeout=0)
    second = manager.acquire('news', timeout=0)
    with pytest.raises(capacity.CapacityBusy):
        manager.acquire('mcp_read', timeout=0)
    third = manager.acquire('recovery', critical=True, timeout=0)
    with pytest.raises(capacity.CapacityBusy):
        manager.acquire('maintenance', critical=True, timeout=0)
    assert manager.snapshot()['active'] == manager.snapshot()['peaks']['active'] == 3
    for lease in (first, second, third):
        lease.release()
        lease.release()
    assert manager.snapshot()['active'] == 0


def test_nested_sequential_borrows_parent_but_parallel_counts_separately(manager):
    with ThreadPoolExecutor(max_workers=2) as pool:
        with capacity.capacity_scope('agent') as parent:
            with capacity.capacity_scope('metadata') as sequential:
                assert sequential is parent and manager.snapshot()['active'] == 1
            result = capacity.submit_capacity(pool, 'news', lambda: manager.snapshot()['active'])
            assert result.result() == 2
        assert manager.snapshot()['active'] == 0


def test_saturated_nested_children_fail_bounded_without_deadlock(manager):
    other = manager.acquire('news', timeout=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with capacity.capacity_scope('agent'):
            start = time.monotonic()
            with pytest.raises(capacity.CapacityBusy):
                capacity.submit_capacity(pool, 'news', lambda: None, timeout=.02)
            assert time.monotonic() - start < .5
    other.release()


def test_running_future_cannot_release_capacity_on_caller_timeout(manager):
    entered, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = capacity.submit_capacity(pool, 'agent', lambda: (entered.set(), finish.wait(1)))
        assert entered.wait(1)
        assert not future.cancel()
        assert manager.snapshot()['active'] == manager.snapshot()['running'] == 1
        finish.set()
        future.result()
    assert manager.snapshot()['active'] == 0


def test_cancelled_executor_queue_releases_only_unstarted_lease(manager):
    entered, finish = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = capacity.submit_capacity(pool, 'agent', lambda: (entered.set(), finish.wait(1)))
        assert entered.wait(1)
        second = capacity.submit_capacity(pool, 'news', lambda: pytest.fail('cancelled worker ran'))
        assert manager.snapshot()['active'] == 2 and manager.snapshot()['running'] == 1
        assert second.cancel()
        assert manager.snapshot()['active'] == 1
        finish.set()
        first.result()
    assert manager.snapshot()['active'] == 0


def test_queue_limit_and_fifo_are_bounded(manager):
    first, second = manager.acquire('agent'), manager.acquire('news')
    ticket_a = manager.enqueue('memory')
    ticket_b = manager.enqueue('metadata')
    with pytest.raises(capacity.CapacityBusy, match='queue_full'):
        manager.enqueue('paper')
    assert manager.snapshot()['queued'] == 2
    first.release()
    assert ticket_b.try_acquire() is None
    lease_a = ticket_a.try_acquire()
    assert lease_a
    lease_a.release()
    lease_b = ticket_b.try_acquire()
    assert lease_b
    lease_b.release()
    second.release()


def test_pressure_rejects_new_analysis_but_keeps_reserved_recovery(manager):
    manager.set_pressure(True, 'memory_high')
    with pytest.raises(capacity.CapacityBusy):
        manager.acquire('agent', timeout=0)
    with capacity.capacity_scope('recovery', critical=True):
        assert manager.snapshot()['pressure']
    manager.set_pressure(False)
    with capacity.capacity_scope('agent'):
        pass


def test_byte_commitments_include_retained_objects_and_refresh_overlap(manager):
    old = capacity.reserve_bytes('catalogue', 40)
    consumer = capacity.reserve_bytes('client', 30)
    with pytest.raises(capacity.CapacityBusy, match='byte_budget'):
        capacity.reserve_bytes('parse', 40)
    refresh = capacity.reserve_bytes('parse', 30)
    assert manager.snapshot()['bytes'] == manager.snapshot()['peaks']['bytes'] == 100
    for lease in (old, consumer, refresh):
        lease.release()
    assert manager.snapshot()['bytes'] == 0


@pytest.mark.parametrize('settings', [dict(total=1, reserved=1), dict(queue_limit=-1),
                                     dict(byte_limit=0), dict(role_limits={'cfg-secret': 1})])
def test_invalid_capacity_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        capacity.CapacityManager(**settings)


def test_zero_queue_means_immediate_admission_only():
    value = capacity.CapacityManager(total=2, reserved=1, queue_limit=0)
    lease = value.acquire('agent', timeout=0)
    with pytest.raises(capacity.CapacityBusy):
        value.acquire('news', timeout=0)
    assert value.snapshot()['peaks']['queued'] == 0
    lease.release()


def test_refresh_space_transfers_to_retained_cache_without_temporary_oversubscription(manager):
    refresh = manager.reserve_bytes('parse', 100)
    retained = refresh.transfer('catalogue', 30)
    assert manager.snapshot()['bytes'] == 100
    assert manager.snapshot()['byte_classes']['parse'] == 70
    refresh.release()
    assert manager.snapshot()['bytes'] == 30
    retained.release()
    assert manager.snapshot()['bytes'] == 0


@pytest.mark.parametrize('raw,expected', [('true', True), ('false', False), ('1', True), ('0', False)])
def test_capacity_mode_is_explicit_and_visible(monkeypatch, raw, expected):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', raw)
    value = capacity.CapacityManager.from_environment()
    assert value.limits_enabled is expected
    assert value.snapshot()['settings']['limits_enabled'] is expected
    assert value.snapshot()['settings']['mode'] == ('enforced' if expected else 'observe_only')


def test_invalid_capacity_mode_is_rejected(monkeypatch):
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'maybe')
    with pytest.raises(ValueError, match='RUNTIME_CAPACITY_ENABLED'):
        capacity.CapacityManager.from_environment()


def test_observation_mode_counts_resources_without_capacity_rejections():
    value = capacity.CapacityManager(total=2, reserved=1, queue_limit=0,
                                     byte_limit=1, limits_enabled=False)
    value.set_pressure(True, 'observed_memory_pressure')
    workers = [value.acquire('agent', timeout=0) for _ in range(5)]
    for worker in workers:
        worker.mark_started()
    retained = value.reserve_bytes('catalogue', 100)
    transient = value.reserve_bytes('parse', 200)
    assert value.snapshot()['active'] == value.snapshot()['running'] == 5
    assert value.snapshot()['bytes'] == value.snapshot()['peaks']['bytes'] == 300
    assert all(row['rejected'] == 0 for row in value.snapshot()['roles'].values())
    for lease in [*workers, retained, transient]:
        lease.release()
    assert value.snapshot()['active'] == value.snapshot()['running'] == value.snapshot()['bytes'] == 0


def test_observation_mode_reads_full_storage_record_above_resource_cap(monkeypatch):
    import sqlite3
    from backend.utils.sqlite_blob import bounded_text
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'false')
    value = capacity.CapacityManager(limits_enabled=False, byte_limit=1)
    monkeypatch.setattr(capacity, '_manager', value)
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE records(payload TEXT)')
        payload = 'complete observation' * 100
        conn.execute('INSERT INTO records VALUES(?)', (payload,))
        with bounded_text(conn, 'records', 'payload', 1, limit=1) as observed:
            assert observed == payload
            assert value.snapshot()['bytes'] >= len(payload) * 8
        assert value.snapshot()['bytes'] == 0


def test_observation_mode_ingests_entire_stream_batch_above_resource_cap(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend.utils.execution_stream import consume
    monkeypatch.setenv('RUNTIME_CAPACITY_ENABLED', 'false')
    monkeypatch.setenv('RUNTIME_STREAM_ITEMS', '1')
    stop = threading.Event()
    trades = [{'id': 'one'}, {'id': 'two'}]
    ingested = []
    class Client:
        closed = False
        async def watch_my_trades(self, symbol):
            stop.set()
            return trades
        async def close(self):
            self.closed = True
    client = Client()
    asyncio.run(consume(client, SimpleNamespace(symbol='ETH/USDT:USDT', ingest=ingested.extend), stop))
    assert ingested == trades
    assert client.closed
