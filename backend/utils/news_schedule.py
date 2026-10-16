"""News polling slots anchored to midnight in China standard time."""
from datetime import datetime, timedelta, timezone


NEWS_TIMEZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')


def news_schedule_slot(now: datetime, interval_seconds: int) -> datetime | None:
    """Return the current due slot during the scheduler's one-minute tick window.

    A late tick never catches up a slot from an earlier minute. Each calendar
    day starts a fresh schedule, including intervals that do not divide a day.
    """
    local = now.astimezone(NEWS_TIMEZONE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (local - midnight).total_seconds()
    slot = midnight + timedelta(seconds=int(elapsed // interval_seconds) * interval_seconds)
    return slot.astimezone(timezone.utc) if local - slot < timedelta(minutes=1) else None
