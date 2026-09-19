from datetime import datetime
from unittest.mock import patch

import pytest
import pytz

from backend.utils.run_schedule import schedule_preview, schedule_due


def at(value):
    return pytz.timezone('Asia/Shanghai').localize(datetime.fromisoformat(value))


@pytest.mark.parametrize('now,expected,frequency', [
    ('2026-09-19T08:09:55', '09-19 08:10', '60m'),
    ('2026-09-19T08:11:00', '09-19 08:30', '20m'),
    ('2026-09-19T15:51:00', '09-19 16:00', '20m'),
    ('2026-09-19T23:59:00', '09-20 00:00', '60m'),
])
def test_dashboard_preview_matches_dispatch_across_windows(now, expected, frequency):
    cfg = {'mode': 'REAL', 'run_interval': 60, 'run_schedule': [{
        'name': 'Weekend', 'days': [5, 6], 'start': '08:10', 'end': '16:00',
        'interval': 20, 'timezone': 'Asia/Shanghai',
    }]}
    preview = schedule_preview(cfg, at(now))
    assert preview['next_run'] == expected
    assert preview['frequency'] == frequency
    assert schedule_due(cfg, datetime.fromisoformat(preview['next_run_at']))
    assert preview['timezone'] == 'Asia/Shanghai'


@pytest.mark.parametrize('enabled,global_enabled', [(False, True), (True, False)])
def test_paused_has_no_next_dispatch(enabled, global_enabled):
    preview = schedule_preview({'enabled': enabled}, at('2026-09-19T10:00'), scheduler_enabled=global_enabled)
    assert preview['state'] == 'paused'
    assert preview['next_run_at'] is None


def test_dca_daily_catch_up_and_completed_period():
    cfg = {'mode': 'SPOT_DCA', 'dca_time': '08:30'}
    assert schedule_preview(cfg, at('2026-09-19T08:00'))['next_run'] == '09-19 08:30'
    assert schedule_preview(cfg, at('2026-09-19T09:00'))['next_run'] == '09-19 09:01'
    assert schedule_preview(cfg, at('2026-09-19T09:00'), dca_executed=True)['next_run'] == '09-20 08:30'


def test_dca_weekly_alias_and_already_executed_week():
    cfg = {'mode': 'SPOT_DCA', 'dca_freq': 'weekly', 'dca_weekday': 4, 'dca_time': '10:15'}
    assert schedule_preview(cfg, at('2026-09-17T12:00'))['next_run'] == '09-18 10:15'
    assert schedule_preview(cfg, at('2026-09-18T12:00'), dca_executed=True)['next_run'] == '09-25 10:15'
    assert schedule_preview(cfg, at('2026-09-17T12:00'), dca_executed=True)['next_run'] == '09-25 10:15'


def test_dca_midnight_catchup_does_not_claim_a_slot_before_next_target():
    cfg = {'mode': 'SPOT_DCA', 'dca_time': '08:30'}
    assert schedule_preview(cfg, at('2026-09-19T23:59:20'))['next_run'] == '09-20 08:30'


def test_weekly_dispatch_deduplication_uses_iso_year(monkeypatch):
    from backend.app.core import scheduler
    monkeypatch.setattr(scheduler, '_last_run_times', {'dca': at('2026-12-31T10:00')})
    assert scheduler.dca_was_dispatched('dca', at('2027-01-01T10:00'), 'weekly')
    assert not scheduler.dca_was_dispatched('dca', at('2027-01-08T10:00'), 'weekly')


def test_service_uses_shared_rules_instead_of_legacy_interval():
    from backend.app.services import dashboard_service
    cfg = {'mode': 'REAL', 'run_interval': 60, 'run_schedule': [{
        'name': 'Window', 'days': [5], 'start': '08:10', 'end': '16:00',
        'interval': 20, 'timezone': 'Asia/Shanghai',
    }]}
    with patch.object(dashboard_service, 'datetime') as clock, patch.object(dashboard_service, 'get_scheduler_status', return_value=True):
        clock.now.return_value = at('2026-09-19T08:15')
        assert dashboard_service.calculate_next_run(cfg) == '09-19 08:30'
