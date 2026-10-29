"""Source-offset preserving time context; no timezone inference or prose rewriting."""
from datetime import datetime, timezone


EVENT_TIME_POLICY_VERSION = 'source-time-v1'
EVENT_TIME_POLICY = """事件时间规则：引用新闻或日历时间时保留来源提供的日期和时区（UTC、明确偏移或美东等），不能只保留钟点，也不能将 UTC 直接视为北京时间。
比较事件与当前时间须按明确时区换算同一瞬间；例如 2026-10-29 12:30 UTC 是北京时间当日20:30，并非北京时间12:30。
保留原时区表达，不默认统一改为北京时间；原文缺少时区或日期时不得猜测补全。已提供的北京时间不要再次加8小时。
区分新闻发布时间、事件预定公布时间及快照采集时间。upcoming 表示预定时刻未到；scheduled_time_reached 只表示时间已到，不证明数据已公布，实际公布须有报道依据。
摘要和记忆压缩不得丢失仍相关事件的日期、时区或已确认的时间修正；旧摘要与新来源冲突时明确纠正，不沿用误记。"""


def explicit_datetime(value: str | None) -> datetime | None:
    """Only timestamps containing both a full date/time and an explicit offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def source_time_text(value: str | None) -> str:
    parsed = explicit_datetime(value)
    if parsed is None:
        return str(value or '')
    offset = parsed.strftime('%z')
    zone = 'UTC' if parsed.utcoffset().total_seconds() == 0 else f'UTC{offset[:3]}:{offset[3:]}'
    return f"{parsed.strftime('%Y-%m-%d %H:%M:%S')} {zone}"


def article_time_fields(item: dict) -> dict:
    """Add readable labels without changing any source timestamp or article text."""
    return {**item, **{f'{key}_display': source_time_text(item[key])
                      for key in ('published_at', 'scheduled_at') if item.get(key)}}


def news_time_context(news: dict, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('News comparison time requires an explicit timezone')
    lines = [f'当前读取时间：{source_time_text(now.isoformat())}']
    if news.get('as_of'):
        lines.append(f"快照采集时间（非事件公布时间）：{source_time_text(news['as_of'])}")
    events = news.get('events') or [item for item in news.get('items', []) if item.get('scheduled_at')]
    for event in events:
        value = event.get('scheduled_at')
        if not value:
            continue
        scheduled = explicit_datetime(value)
        relation = ('upcoming（预定时刻未到）' if scheduled > now else
                    'scheduled_time_reached（预定时刻已到，不证明实际公布）') if scheduled else '时间/时区不明确，无法比较'
        lines.append(f"- {event.get('title', '')}：预定时间 {source_time_text(value)}；{relation}")
    return '\n'.join(lines)
