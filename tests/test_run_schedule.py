from datetime import datetime, timedelta
import unittest

import pytz
from pydantic import ValidationError

from backend.utils.run_schedule import (
    RunScheduleRule, effective_schedule, next_scheduled_run,
    schedule_due, validate_run_schedule,
)


def at(value, timezone='Asia/Shanghai'):
    return pytz.timezone(timezone).localize(datetime.fromisoformat(value))


def rule(**overrides):
    return RunScheduleRule.model_validate({
        'name': 'Asia', 'days': [0, 1, 2, 3, 4], 'start': '08:10',
        'end': '16:00', 'interval': 30, 'timezone': 'Asia/Shanghai',
        **overrides,
    }).model_dump()


class RunScheduleTests(unittest.TestCase):
    def test_interval_is_anchored_to_start_and_end_is_exclusive(self):
        config = {'run_interval': 60, 'run_schedule': [rule()]}
        self.assertTrue(schedule_due(config, at('2026-09-07T08:10')))
        self.assertTrue(schedule_due(config, at('2026-09-07T08:40')))
        self.assertFalse(schedule_due(config, at('2026-09-07T08:30')))
        self.assertEqual(effective_schedule(config, at('2026-09-07T16:00'))['rule_index'], None)

    def test_first_rule_overrides_us_session_on_beijing_weekend(self):
        config = {'run_schedule': [
            rule(name='Weekend', days=[5, 6], start='00:00', end='24:00'),
            rule(name='US', timezone='America/New_York', start='09:30', end='16:00', interval=20),
        ]}
        self.assertEqual(effective_schedule(config, at('2026-09-12T01:00'))['name'], 'Weekend')
        self.assertEqual(effective_schedule(config, at('2026-09-11T22:00'))['name'], 'US')

    def test_overnight_uses_start_weekday(self):
        config = {'run_schedule': [rule(days=[4], start='22:00', end='02:00')]}
        self.assertEqual(effective_schedule(config, at('2026-09-12T01:30'))['rule_index'], 0)
        self.assertIsNone(effective_schedule(config, at('2026-09-12T02:00'))['rule_index'])
        self.assertIsNone(effective_schedule(config, at('2026-09-11T01:30'))['rule_index'])

    def test_new_york_dst_changes_beijing_open(self):
        config = {'run_schedule': [rule(timezone='America/New_York', start='09:30', end='16:00', interval=20)]}
        for value in ['2026-07-06T21:30', '2026-12-07T22:30']:
            self.assertEqual(effective_schedule(config, at(value))['elapsed'], 0)
            self.assertTrue(schedule_due(config, at(value)))

    def test_dst_missing_start_and_repeated_hour(self):
        config = {'run_schedule': [rule(days=[6], timezone='America/New_York', start='02:30', end='04:00')]}
        self.assertEqual(effective_schedule(config, at('2026-03-08T03:00', 'America/New_York'))['elapsed'], 0)
        config['run_schedule'][0]['start'] = '01:00'
        zone = pytz.timezone('America/New_York')
        first = zone.localize(datetime(2026, 11, 1, 1, 30), is_dst=True)
        second = zone.localize(datetime(2026, 11, 1, 1, 30), is_dst=False)
        self.assertEqual(effective_schedule(config, first)['elapsed'], 30)
        self.assertEqual(effective_schedule(config, second)['elapsed'], 90)

    def test_next_run_crosses_window_boundary_and_matches_execution(self):
        from backend.app.core import scheduler
        config = {'config_id': 'schedule-test', 'run_interval': 60, 'run_schedule': [rule(interval=20)]}
        now = at('2026-09-07T08:09:59')
        scheduler._last_run_times.pop('schedule-test', None)
        for _ in range(30):
            next_run = next_scheduled_run(config, now)
            self.assertGreater(next_run, now)
            self.assertTrue(scheduler.is_time_to_run(config, next_run))
            self.assertFalse(any(schedule_due(config, now.replace(second=0) + timedelta(minutes=i))
                                 for i in range(1, int((next_run - now).total_seconds() // 60))))
            now = next_run

    def test_legacy_schedule_and_same_minute_deduplication(self):
        from backend.app.core import scheduler
        config = {'config_id': 'schedule-test', 'mode': 'REAL', 'run_interval': 20}
        now = at('2026-09-07T08:20')
        scheduler._last_run_times['schedule-test'] = now - timedelta(days=1)
        try:
            self.assertTrue(scheduler.is_time_to_run(config, now))
            scheduler._last_run_times['schedule-test'] = now
            self.assertFalse(scheduler.is_time_to_run(config, now + timedelta(seconds=20)))
        finally:
            scheduler._last_run_times.pop('schedule-test', None)

    def test_validation_rejects_invalid_windows(self):
        for overrides in [{'days': []}, {'days': [7]}, {'days': [1, 1]},
                          {'start': '24:00'}, {'end': '25:00'},
                          {'start': '08:00', 'end': '08:00'},
                          {'interval': 14}, {'interval': 1441}, {'timezone': 'invalid'}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                rule(**overrides)
        with self.assertRaises(ValueError):
            validate_run_schedule([rule()] * 21)

    def test_api_and_config_normalization_preserve_rules(self):
        from backend.app.schemas.payloads import ConfigAgentPayload
        from backend.config_store import _normalize_agents
        rules = [rule()]
        payload = ConfigAgentPayload(config_id='schedule-test', symbol='BTC/USDT', run_schedule=rules)
        normalized = _normalize_agents([payload.model_dump()])[0]
        self.assertEqual(normalized['run_schedule'], rules)
        self.assertEqual(_normalize_agents([{'config_id': 'legacy'}])[0]['run_schedule'], [])


if __name__ == '__main__':
    unittest.main()
