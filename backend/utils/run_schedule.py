"""Shared interval policy for scheduler execution and next-run prompt previews."""
from datetime import datetime, timedelta
import re

import pytz
from pydantic import BaseModel, Field, field_validator, model_validator


class RunScheduleRule(BaseModel):
    name: str = Field(default='时段', max_length=80)
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = '00:00'
    end: str = '24:00'
    interval: int = Field(ge=15, le=1440)
    timezone: str = 'Asia/Shanghai'

    @field_validator('days')
    @classmethod
    def valid_days(cls, values):
        if any(day not in range(7) for day in values) or len(set(values)) != len(values):
            raise ValueError('星期使用0（周一）至6（周日），不可重复')
        return values

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        if value not in pytz.all_timezones_set:
            raise ValueError('未知时区')
        return value

    @model_validator(mode='after')
    def valid_window(self):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.start):
            raise ValueError('开始时间须为00:00至23:59')
        if self.end != '24:00' and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', self.end):
            raise ValueError('结束时间须为00:00至24:00')
        if self.start == self.end:
            raise ValueError('全天请使用00:00—24:00')
        return self


def validate_run_schedule(rules):
    if len(rules or []) > 20:
        raise ValueError('最多20条运行时段规则')
    return [RunScheduleRule.model_validate(rule).model_dump() for rule in (rules or [])]


def _minutes(text):
    hour, minute = map(int, text.split(':'))
    return hour * 60 + minute


def effective_schedule(config, now):
    """First matching rule wins; overnight weekdays refer to the start date."""
    for index, rule in enumerate(config.get('run_schedule') or []):
        local = now.astimezone(pytz.timezone(rule['timezone']))
        minute = local.hour * 60 + local.minute
        start, end = _minutes(rule['start']), _minutes(rule['end'])
        date = local.date()
        if start < end:
            if not start <= minute < end:
                continue
        elif minute < end:
            date -= timedelta(days=1)
        elif minute < start:
            continue
        if date.weekday() not in rule['days']:
            continue
        naive = datetime.combine(date, datetime.min.time()) + timedelta(minutes=start)
        timezone = pytz.timezone(rule['timezone'])
        try:
            anchor = timezone.localize(naive, is_dst=None)
        except pytz.AmbiguousTimeError:
            anchor = timezone.localize(naive, is_dst=True)
        except pytz.NonExistentTimeError:
            # Spring-forward: start at the first real minute of this window.
            while True:
                naive += timedelta(minutes=1)
                try:
                    anchor = timezone.localize(naive, is_dst=None)
                    break
                except pytz.NonExistentTimeError:
                    pass
        elapsed = int((now.timestamp() - anchor.timestamp()) // 60)
        return {'interval': rule['interval'], 'elapsed': elapsed, 'name': rule['name'], 'rule_index': index}
    default = 60 if config.get('mode', 'STRATEGY').upper() == 'STRATEGY' else 15
    try:
        interval = max(15, min(1440, int(config.get('run_interval') or default)))
    except (ValueError, TypeError):
        interval = default
    return {'interval': interval, 'elapsed': now.hour * 60 + now.minute, 'name': '默认间隔', 'rule_index': None}


def schedule_due(config, now):
    policy = effective_schedule(config, now)
    return policy['elapsed'] >= 0 and policy['elapsed'] % policy['interval'] == 0


def next_scheduled_run(config, now):
    candidate = now.astimezone(pytz.UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 1440):
        local = candidate.astimezone(now.tzinfo)
        if schedule_due(config, local):
            return local
        candidate += timedelta(minutes=1)
    return None
