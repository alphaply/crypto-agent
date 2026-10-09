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
    default = 60 if config.get('market_profile') == 'hourly' or str(config.get('mode', 'STRATEGY')).upper() == 'STRATEGY' else 15
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


def normalize_dca_freq(raw_freq):
    return '1w' if str(raw_freq or '1d').strip().lower() in {'1w', 'weekly', 'week', 'w'} else '1d'


def parse_dca_time(raw_time):
    try:
        parts = str(raw_time or '08:00').strip().split(':')
        return min(23, max(0, int(parts[0]))), min(59, max(0, int(parts[1]) if len(parts) > 1 else 0))
    except (ValueError, TypeError):
        return 8, 0


class DcaSchedule(BaseModel):
    frequency: str = 'daily'
    days: list[int] = Field(default_factory=lambda: [0])
    times: list[str] = Field(default_factory=lambda: ['09:00'], min_length=1, max_length=24)
    timezone: str = 'Asia/Shanghai'

    @model_validator(mode='after')
    def validate_schedule(self):
        if self.frequency not in {'daily', 'weekly'}:
            raise ValueError('frequency must be daily or weekly')
        if self.timezone not in pytz.all_timezones_set:
            raise ValueError('未知时区')
        if not self.days or any(day not in range(7) for day in self.days):
            raise ValueError('星期使用0（周一）至6（周日）')
        if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item) for item in self.times):
            raise ValueError('执行时间须为00:00至23:59')
        if len(set(self.times)) != len(self.times) or len(set(self.days)) != len(self.days):
            raise ValueError('执行时间和星期不能重复')
        self.times = sorted(self.times)
        self.days = sorted(self.days)
        return self


def dca_schedule(config):
    if config.get('dca_schedule'):
        return DcaSchedule.model_validate(config['dca_schedule'])
    hour, minute = parse_dca_time(config.get('dca_time'))
    return DcaSchedule(frequency='weekly' if normalize_dca_freq(config.get('dca_freq')) == '1w' else 'daily',
                       days=[int(config.get('dca_weekday', 0))], times=[f'{hour:02d}:{minute:02d}'])


def dca_slots(config, now, *, future=False, count=3):
    """Calendar slots use UTC identities, including across DST and process restarts."""
    schedule = dca_schedule(config)
    zone = pytz.timezone(schedule.timezone)
    local = now.astimezone(zone)
    slots = []
    for offset in (range(max(15, min(int(count), 20) * 7 + 1)) if future else range(-7, 1)):
        date = local.date() + timedelta(days=offset)
        if schedule.frequency == 'weekly' and date.weekday() not in schedule.days:
            continue
        for clock in schedule.times:
            naive = datetime.combine(date, datetime.min.time()) + timedelta(minutes=_minutes(clock))
            try:
                value = zone.localize(naive, is_dst=None)
            except pytz.AmbiguousTimeError:
                value = zone.localize(naive, is_dst=True)  # One execution for a repeated local slot.
            except pytz.NonExistentTimeError:
                continue
            if (value > now) if future else (value <= now):
                slots.append(value)
    return sorted(slots)


def latest_dca_slot(config, now):
    schedule = dca_schedule(config)
    local = now.astimezone(pytz.timezone(schedule.timezone))
    slots = dca_slots(config, now)
    slots = [slot for slot in slots if (slot.date() == local.date() if schedule.frequency == 'daily'
                                       else slot.isocalendar()[:2] == local.isocalendar()[:2])]
    return slots[-1] if slots else None


def dca_cycle_id(config_id, slot):
    return f'scheduled:{config_id}:{slot.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}'


def preview_dca_schedule(config, now=None, count=3):
    now = now or datetime.now(pytz.timezone(dca_schedule(config).timezone))
    return [slot.isoformat() for slot in dca_slots(config, now, future=True, count=count)[:max(1, min(int(count), 20))]]


def schedule_preview(config, now, *, scheduler_enabled=True, dca_executed=False):
    """Describe nominal dispatch times, not a guarantee of worker completion."""
    mode = str(config.get('mode', 'STRATEGY')).upper()
    state = 'scheduled' if scheduler_enabled and config.get('enabled', True) else 'paused'
    if mode == 'SPOT_DCA':
        schedule = dca_schedule(config)
        future_slots = dca_slots(config, now, future=True)
        latest = latest_dca_slot(config, now)
        next_run = (now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                    if latest and not dca_executed else (future_slots[0] if future_slots else None))
        if next_run and latest and not dca_executed:
            local_next = next_run.astimezone(pytz.timezone(schedule.timezone))
            same_period = (local_next.date() == latest.date() if schedule.frequency == 'daily'
                           else local_next.isocalendar()[:2] == latest.isocalendar()[:2])
            if not same_period:
                next_run = future_slots[0] if future_slots else None
        return {
            'state': state, 'frequency': f'{schedule.frequency} ' + ', '.join(schedule.times),
            'rule_name': 'DCA', 'rule_index': None, 'timezone': schedule.timezone,
            'next_run_at': next_run.isoformat() if next_run and state != 'paused' else None,
            'next_run': next_run.strftime('%m-%d %H:%M') if next_run and state != 'paused' else '—',
            'rules': [], 'default_interval': 60, 'dca_schedule': schedule.model_dump(),
        }
    else:
        policy = effective_schedule(config, now)
        frequency = f"{policy['interval']}m"
        rule_name, rule_index = policy['name'], policy['rule_index']
        next_run = next_scheduled_run(config, now)
    if state == 'paused':
        next_run = None
    return {
        'state': state,
        'frequency': frequency,
        'rule_name': rule_name,
        'rule_index': rule_index,
        'timezone': str(now.tzinfo),
        'next_run_at': next_run.isoformat() if next_run else None,
        'next_run': next_run.strftime('%m-%d %H:%M') if next_run else '—',
        'rules': (config.get('run_schedule') or []) if mode != 'SPOT_DCA' else [],
        'default_interval': effective_schedule({**config, 'run_schedule': []}, now)['interval'],
    }
