from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from backend.utils import task_stall


def test_stalled_worker_warns_with_stack_without_being_finished(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(task_stall.time, 'monotonic', lambda: clock[0])
    logger = Mock()
    with task_stall.track_task('task', 'scheduled') as progress:
        clock[0] = 599
        task_stall.warn_stalled_tasks(logger)
        logger.error.assert_not_called()
        clock[0] = 600
        task_stall.warn_stalled_tasks(logger)
        assert logger.error.call_count == 1
        assert logger.error.call_args.args[1:5] == ('task', 'scheduled', 'preparing', 600)
        assert 'test_stalled_worker_warns' in logger.error.call_args.args[-1]
        assert ('task', 'scheduled') in task_stall._active
        clock[0] = 660
        task_stall.warn_stalled_tasks(logger)
        assert logger.error.call_count == 1
        progress({'phase': 'thinking'})
        clock[0] = 1200
        task_stall.warn_stalled_tasks(logger)
        assert logger.error.call_count == 1
        clock[0] = 1260
        task_stall.warn_stalled_tasks(logger)
        assert logger.error.call_count == 2
        assert logger.error.call_args.args[3] == 'thinking'
    clock[0] += 10000
    task_stall.warn_stalled_tasks(logger)
    assert logger.error.call_count == 2
    assert ('task', 'scheduled') not in task_stall._active


def test_failed_task_removes_watchdog_registration():
    with pytest.raises(RuntimeError):
        with task_stall.track_task('failed', 'scheduled'):
            raise RuntimeError('fixture')
    assert ('failed', 'scheduled') not in task_stall._active


@pytest.mark.parametrize('status,age,stalled', [
    ('RUNNING', 599, False), ('RUNNING', 600, True), ('RUNNING', 16 * 3600, True),
    ('RUNNING', -10, False), ('FINISHED', 3600, False), ('FAILED', 3600, False),
])
def test_dashboard_uses_last_progress_and_preserves_execution(status, age, stalled, monkeypatch):
    from backend.app.services import agent_runtime_view as views
    current = views.TZ_CN.localize(datetime(2026, 11, 15, 16, 0))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    monkeypatch.setattr(views, 'datetime', Clock)
    row = {'status': status, 'phase': 'preparing',
           'started_at': '2026-11-14 23:30:00',
           'updated_at': (current - timedelta(seconds=age)).strftime('%Y-%m-%d %H:%M:%S')}
    result = views.execution_view(row)
    assert result['stalled'] is stalled
    assert result['status'] == status
    assert result['phase'] == 'preparing'
    assert 'stalled' not in row


@pytest.mark.parametrize('value', [None, 'broken'])
def test_missing_progress_time_is_not_invented(value):
    from backend.app.services.agent_runtime_view import execution_view
    result = execution_view({'status': 'RUNNING', 'updated_at': value})
    assert result['idle_seconds'] is None
    assert result['stalled'] is False
