"""BlockBeats Pro JSON newsflash API; credentials never enter source URLs/cache."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from backend.app.schemas.news import BLOCKBEATS_API_URL
from backend.utils.news_context import _clean_text, _make_item


class BlockBeatsError(ValueError):
    """Safe source diagnostics without provider bodies or request credentials."""


def _parse_item(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    identifier = str(row.get('id') or '')
    title = _clean_text(row.get('title'))
    if not identifier.isdigit() or not title:
        return None
    try:
        published = datetime.fromisoformat(str(row.get('create_time') or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    # The newsflash API's wall-clock timestamps use BlockBeats' Beijing time.
    if published.tzinfo is None:
        published = published.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    link = str(row.get('link') or '')
    try:
        parsed_link = urlsplit(link)
        valid_link = parsed_link.scheme in {'http', 'https'} and bool(parsed_link.hostname)
    except ValueError:
        valid_link = False
    if not valid_link:
        link = f'https://www.theblockbeats.info/flash/{identifier}'
    return {
        **_make_item(source='BlockBeats', title=title, category='crypto', impact='medium',
                     relevance=0.8, published_at=published, url=link),
        'id': f'blockbeats:{identifier}', 'guid': f'blockbeats:{identifier}',
        'content': _clean_text(row.get('content')),
    }


def fetch_newsflash(api_key: str, *, language: str = 'cn', limit: int = 80,
                   now: datetime | None = None, lookback_hours: int = 24,
                   timeout: float = 6.0) -> list[dict]:
    """Fetch bounded pages with the API key in a header, without redirects."""
    if not str(api_key or '').strip():
        raise BlockBeatsError('Configure the BlockBeats API key in News aggregation')
    limit = max(1, min(int(limit), 300))
    size = min(limit, 50)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    deadline = time.monotonic() + 12.0
    items: dict[str, dict] = {}
    for page in range(1, (limit + size - 1) // size + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BlockBeatsError('BlockBeats API timed out')
        try:
            response = httpx.get(
                BLOCKBEATS_API_URL, params={'page': page, 'size': size, 'lang': language or 'cn'},
                headers={'api-key': api_key.strip(), 'Accept': 'application/json'},
                timeout=min(timeout, remaining), follow_redirects=False,
            )
        except httpx.RequestError:
            raise BlockBeatsError('BlockBeats API connection failed') from None
        if response.status_code != 200:
            raise BlockBeatsError(f'BlockBeats API HTTP {response.status_code}; check credentials and quota')
        if len(response.content) > 8 * 1024 * 1024:
            raise BlockBeatsError('BlockBeats API response is too large')
        try:
            payload = response.json()
        except ValueError:
            raise BlockBeatsError('BlockBeats API returned invalid JSON') from None
        if not isinstance(payload, dict) or type(payload.get('status')) is not int or payload['status'] != 0:
            raise BlockBeatsError('BlockBeats API rejected the request; check credentials and quota')
        data = payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise BlockBeatsError('BlockBeats API returned an invalid newsflash list')
        rows = data['data']
        parsed = [item for row in rows if (item := _parse_item(row)) is not None]
        if rows and not parsed:
            raise BlockBeatsError('BlockBeats API returned invalid newsflash records')
        before = len(items)
        for item in parsed:
            published = datetime.fromisoformat(item['published_at'])
            if cutoff <= published <= now + timedelta(minutes=10):
                items[item['id']] = item
        if len(rows) < size or len(items) >= limit or len(items) == before:
            break
        if parsed and all(datetime.fromisoformat(item['published_at']) < cutoff for item in parsed):
            break
    return list(items.values())[:limit]
