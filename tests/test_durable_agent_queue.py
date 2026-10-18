"""Small isolated PostgreSQL checks; no model or exchange calls."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend import storage
from backend.storage import agent_queue as queue


def task(name='a', **changes):
    return {'config_id': name, 'symbol': 'BTC/USDT', 'mode': 'STRATEGY',
            'enabled': True, 'model': 'old', 'exchange': 'binance', **changes}


@pytest.fixture
def runtime(pg, monkeypatch):
    snapshot = {'enable_scheduler': True, 'agents': [task(), task('b')]}
    monkeypatch.setattr(queue, '_snapshot', lambda conn: snapshot)
    return snapshot


def put(name='a', number=1, *, kind='scheduled', run_id=None):
    return queue.enqueue(name, f'{kind}:{number}', kind, '2026-10-18 08:00:00',
                         run_id or f'{name}:{kind}:{number}')


def test_every_trigger_is_durable_and_only_same_identity_deduplicates(runtime):
    for number in range(40):
        assert put(number=number)
    assert not put(number=1)
    assert put(number=1, kind='news')
    state = queue.get_runtime_states(['a', 'never'])
    assert state['a']['queued_count'] == 41
    assert state['a']['next_queued']['trigger_type'] == 'scheduled'
    assert state['never'] == {'running': None, 'queued_count': 0, 'next_queued': None, 'last_run': None}


def test_oldest_runnable_preserves_same_task_order_without_blocking_other_task(runtime):
    put(number=1)
    put(number=2)
    put('b', 3)
    first = queue.claim_next('worker')
    assert first['config_id'] == 'a'
    assert queue.start_claimed(first['id'], 'worker')
    second = queue.claim_next('worker')
    assert second['config_id'] == 'b'
    assert queue.claim_next('worker') is None
    state = queue.get_runtime_state('a')
    assert state['running']['status'] == 'RUNNING' and state['queued_count'] == 1
    queue.finish(first['id'], 'worker', 'FINISHED')
    assert queue.claim_next('worker')['trigger_key'] == 'scheduled:2'


def test_competing_claimers_never_claim_two_rounds_of_the_same_task(runtime):
    from concurrent.futures import ThreadPoolExecutor
    put(number=1)
    put(number=2)
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(queue.claim_next, ['worker-one', 'worker-two']))
    assert sum(row is not None for row in rows) == 1
    assert queue.get_runtime_state('a')['queued_count'] == 2


def test_start_reads_latest_model_and_amount_but_keeps_original_dca_slot(runtime):
    runtime['agents'][0].update(mode='SPOT_DCA', dca_amount=100, dca_budget=1000)
    original = 'scheduled:a:2026-10-18T00:00:00Z'
    put(kind='dca', run_id=original)
    runtime['agents'][0].update(model='new', dca_amount=75)
    claimed = queue.claim_next('worker')
    config, queued = queue.start_claimed(claimed['id'], 'worker')
    assert config['model'] == 'new' and config['dca_amount'] == 75
    assert queued['run_id'] == original
    from backend.utils.spot_execution import ensure_spot_budget_cycle
    ensure_spot_budget_cycle('a', config, queued['run_id'], 'scheduled')
    ensure_spot_budget_cycle('a', config, queued['run_id'], 'scheduled')
    with storage.get_connection() as conn:
        rows = conn.execute('SELECT cycle_id,budget FROM spot_budget_cycles WHERE config_id=%s', ('a',)).fetchall()
    assert len(rows) == 1 and rows[0]['cycle_id'] == original and rows[0]['budget'] == 75


@pytest.mark.parametrize('changes', [{'enabled': False}, {'symbol': 'ETH/USDT'},
                                    {'exchange': 'okx'}, {'mode': 'REAL'}, {'physical_account_id': 'other'}])
def test_configuration_lifecycle_cancels_unstarted_but_preserves_running(runtime, changes):
    previous = {**runtime, 'agents': [dict(item) for item in runtime['agents']]}
    put(number=1)
    claimed = queue.claim_next('worker')
    queue.start_claimed(claimed['id'], 'worker')
    put(number=2)
    runtime['agents'][0].update(changes)
    with storage.get_connection() as conn:
        queue.synchronize_configuration(conn, previous, runtime)
        conn.commit()
    state = queue.get_runtime_state('a')
    assert state['queued_count'] == 0 and state['running']['status'] == 'RUNNING'
    assert state['last_run']['status'] == 'CANCELLED'


def test_deleted_or_global_paused_configs_cancel_unstarted(runtime):
    previous = {**runtime, 'agents': [dict(item) for item in runtime['agents']]}
    put()
    put('b')
    runtime['agents'] = [runtime['agents'][1]]
    with storage.get_connection() as conn:
        queue.synchronize_configuration(conn, previous, runtime)
        conn.commit()
    assert queue.get_runtime_state('a')['last_run']['error'] == 'configuration_deleted'
    runtime['enable_scheduler'] = False
    with storage.get_connection() as conn:
        queue.synchronize_configuration(conn, previous, runtime)
        conn.commit()
    assert queue.get_runtime_state('b')['queued_count'] == 0


def test_restart_recovers_only_unstarted_work_and_never_replays_running(runtime):
    put()
    put('b')
    running = queue.claim_next('old-process')
    queue.start_claimed(running['id'], 'old-process')
    claimed = queue.claim_next('old-process')
    queue.recover_after_restart()
    state = queue.get_runtime_states(['a', 'b'])
    assert state['a']['running'] is None and state['a']['last_run']['status'] == 'INTERRUPTED'
    assert state['b']['queued_count'] == 1
    recovered = queue.claim_next('new-process')
    assert recovered['id'] == claimed['id'] and recovered['run_id'] == claimed['run_id']
    assert queue.claim_next('new-process') is None


def test_news_identity_and_count_commit_with_queue_and_regular_run_coexists(runtime):
    from backend.database import news as database_news
    from backend.app.services.news_triggers import _enqueue_news
    put()
    name = 'news-trigger:a'
    owner = database_news.acquire_lease(name)
    event = {'snapshot_id': 'news-snapshot', 'articles': [{'title': 'CPI new', 'content': 'New report'}]}
    config = {**runtime['agents'][0], '_news_event': event}
    rule = SimpleNamespace(provider_id='news-provider')
    now = datetime.now(timezone.utc)
    assert _enqueue_news(config, rule, 'rule', now, name, owner, {'count': 1, 'seen': ['event']})
    assert not _enqueue_news(config, rule, 'rule', now, name, owner, {'count': 2})
    assert database_news.read_state(name)['count'] == 1
    assert queue.get_runtime_state('a')['queued_count'] == 2


def test_configuration_save_cancels_queue_in_same_transaction(pg, monkeypatch):
    from backend.config_store import save_runtime_snapshot
    from backend.utils import position_management
    monkeypatch.setattr(position_management, 'synchronize_configuration', lambda *args: None)
    save_runtime_snapshot({'enable_scheduler': True}, [task()], [], [])
    assert put()
    def failed_save(conn):
        raise ValueError('isolated save failure')
    with pytest.raises(ValueError, match='isolated save failure'):
        save_runtime_snapshot({'enable_scheduler': True}, [task(enabled=False)], [], [], transaction_hook=failed_save)
    assert queue.get_runtime_state('a')['queued_count'] == 1
    save_runtime_snapshot({'enable_scheduler': True}, [task(enabled=False)], [], [])
    state = queue.get_runtime_state('a')
    assert state['queued_count'] == 0 and state['last_run']['status'] == 'CANCELLED'


def test_capacity_busy_keeps_trigger_queued_and_drain_runs_when_available(runtime, monkeypatch):
    from backend.app.core import scheduler
    from backend.utils import runtime_capacity
    manager = runtime_capacity.CapacityManager(total=2, reserved=0, role_limits={'agent': 1})
    monkeypatch.setattr(scheduler, 'get_capacity_manager', lambda: manager)
    monkeypatch.setattr(scheduler, '_running_agent_futures', {})
    put()
    occupied = manager.acquire('agent', timeout=0)
    assert scheduler.dispatch_agent_queue() == 0
    assert queue.get_runtime_state('a')['queued_count'] == 1
    occupied.release()
    monkeypatch.setattr(scheduler.global_config, 'reload_config', lambda: None)
    monkeypatch.setattr(scheduler, 'run_config_agent', lambda config, scheduled_at, queued_run:
                        queue.finish(queued_run['id'], scheduler._queue_owner, 'FINISHED'))
    assert scheduler.dispatch_agent_queue() == 1
    for future in list(scheduler._running_agent_futures.values()):
        future.result(timeout=5)
    assert queue.get_runtime_state('a')['last_run']['status'] == 'FINISHED'


def test_cancelled_unstarted_future_returns_its_claim_and_capacity(runtime, monkeypatch):
    from concurrent.futures import Future
    from backend.app.core import scheduler
    from backend.utils import runtime_capacity
    manager = runtime_capacity.CapacityManager(total=2, reserved=0, role_limits={'agent': 1})
    pending = Future()
    monkeypatch.setattr(scheduler, 'get_capacity_manager', lambda: manager)
    monkeypatch.setattr(scheduler, '_running_agent_futures', {})
    monkeypatch.setattr(scheduler, '_get_agent_executor', lambda: SimpleNamespace(submit=lambda *args: pending))
    put()
    assert scheduler.dispatch_agent_queue() == 1
    assert pending.cancel()
    assert manager.snapshot()['active'] == 0
    item = queue.claim_next('replacement')
    assert item and item['trigger_key'] == 'scheduled:1'
