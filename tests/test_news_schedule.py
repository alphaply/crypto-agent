from contextlib import nullcontext
from datetime import datetime

import pytest

from backend import database_news
from backend.app.services import news_service
from backend.utils.news_schedule import news_schedule_slot


@pytest.mark.parametrize('local,minutes,expected', [
    ('2026-10-16T10:00:00+08:00', 60, '2026-10-16T02:00:00+00:00'),
    ('2026-10-16T10:00:59.999999+08:00', 60, '2026-10-16T02:00:00+00:00'),
    ('2026-10-16T10:01:00+08:00', 60, None),
    ('2026-10-16T10:17:00+08:00', 60, None),
    ('2026-10-16T10:30:00+08:00', 30, '2026-10-16T02:30:00+00:00'),
    ('2026-10-16T10:00:00+08:00', 30, '2026-10-16T02:00:00+00:00'),
    ('2026-10-16T12:30:00+08:00', 30, '2026-10-16T04:30:00+00:00'),
    ('2026-10-16T13:00:00+08:00', 30, '2026-10-16T05:00:00+00:00'),
    ('2026-10-16T13:30:00+08:00', 30, '2026-10-16T05:30:00+00:00'),
    ('2026-10-16T10:17:00+08:00', 30, None),
    ('2026-10-16T00:45:00+08:00', 45, '2026-10-15T16:45:00+00:00'),
    ('2026-10-16T01:30:00+08:00', 45, '2026-10-15T17:30:00+00:00'),
    ('2026-10-16T01:00:00+08:00', 45, None),
    ('2026-10-16T03:00:00+08:00', 90, '2026-10-15T19:00:00+00:00'),
    ('2026-10-16T23:20:00+08:00', 100, '2026-10-16T15:20:00+00:00'),
    ('2026-10-17T00:00:00+08:00', 100, '2026-10-16T16:00:00+00:00'),
    ('2026-10-16T00:00:00+08:00', 1440, '2026-10-15T16:00:00+00:00'),
    ('2026-10-16T10:17:00+08:00', 1, '2026-10-16T02:17:00+00:00'),
])
def test_news_slots_follow_china_midnight_without_catchup(local, minutes, expected):
    slot = news_schedule_slot(datetime.fromisoformat(local), minutes * 60)
    assert (slot.isoformat() if slot else None) == expected


@pytest.fixture
def scheduled_news(pg, monkeypatch):
    clock = {'now': datetime.fromisoformat('2026-10-16T10:17:00+08:00')}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['now'].astimezone(tz) if tz else clock['now'].replace(tzinfo=None)

    monkeypatch.setattr(news_service, 'datetime', Clock)
    monkeypatch.setattr(database_news, 'datetime', Clock)
    settings = {'news': {'enabled': True, 'refresh_seconds': 3600}}
    monkeypatch.setattr(news_service, '_runtime', lambda: settings)
    monkeypatch.setattr(news_service, 'capacity_scope', lambda *a, **kw: nullcontext())
    attempts = []

    def run(owner, snapshot):
        attempts.append(clock['now'])
        database_news.update_state('news', {
            'last_attempt_at': database_news.now_iso(), 'status': 'error',
        }, owner=owner, release=True)

    monkeypatch.setattr(news_service, '_run_refresh', run)
    return clock, settings['news'], attempts


def poll():
    return news_service.refresh_news(force=False, background=False)['started']


def test_startup_and_changed_interval_wait_for_next_slot(scheduled_news):
    clock, settings, attempts = scheduled_news
    assert not poll()
    settings['refresh_seconds'] = 1800
    assert not poll()
    clock['now'] = datetime.fromisoformat('2026-10-16T10:30:00+08:00')
    assert poll()
    assert not poll()  # Includes an attempt exactly at the boundary, even on error.
    clock['now'] = datetime.fromisoformat('2026-10-16T10:31:00+08:00')
    assert not poll()
    clock['now'] = datetime.fromisoformat('2026-10-16T11:00:07+08:00')
    assert poll()
    assert len(attempts) == 2


def test_manual_refresh_does_not_shift_regular_slots(scheduled_news):
    clock, settings, attempts = scheduled_news
    settings['refresh_seconds'] = 1800
    clock['now'] = datetime.fromisoformat('2026-10-16T10:29:00+08:00')
    assert news_service.refresh_news(background=False)['started']
    clock['now'] = datetime.fromisoformat('2026-10-16T10:30:00+08:00')
    assert poll()
    assert len(attempts) == 2


def test_changed_interval_uses_midnight_not_previous_attempt(scheduled_news):
    clock, settings, attempts = scheduled_news
    clock['now'] = datetime.fromisoformat('2026-10-16T10:00:00+08:00')
    assert poll()
    settings['refresh_seconds'] = 45 * 60
    clock['now'] = datetime.fromisoformat('2026-10-16T10:17:00+08:00')
    assert not poll()
    clock['now'] = datetime.fromisoformat('2026-10-16T10:30:00+08:00')
    assert poll()
    clock['now'] = datetime.fromisoformat('2026-10-16T11:15:00+08:00')
    assert poll()
    assert len(attempts) == 3


def test_missed_slot_is_not_replayed_and_disabled_news_stays_idle(scheduled_news):
    clock, settings, attempts = scheduled_news
    clock['now'] = datetime.fromisoformat('2026-10-16T11:01:00+08:00')
    assert not poll()
    settings['enabled'] = False
    clock['now'] = datetime.fromisoformat('2026-10-16T12:00:00+08:00')
    assert not poll()
    assert not attempts


def test_live_lease_blocks_scheduled_refresh(scheduled_news):
    clock, settings, attempts = scheduled_news
    clock['now'] = datetime.fromisoformat('2026-10-16T11:00:00+08:00')
    owner = database_news.acquire_lease('news')
    assert not poll()
    assert not attempts
    database_news.update_state('news', {}, owner=owner, release=True)
    assert poll()
