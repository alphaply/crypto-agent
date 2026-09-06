"""Read-only Polymarket event snapshots. No credentials or order placement."""
from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def event_slug(value: str) -> str:
    value = value.strip()
    if '://' in value:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or parsed.hostname not in {'polymarket.com', 'www.polymarket.com'} or parsed.username or parsed.port:
            raise ValueError('Use an HTTPS polymarket.com event URL or an event slug')
        match = re.fullmatch(r'/(?:[a-z]{2}/)?event/([a-z0-9-]+)/?', parsed.path)
        if not match:
            raise ValueError('Use an event URL, for example https://polymarket.com/event/fed-decision-in-september-762')
        value = match.group(1)
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value) or len(value) > 200:
        raise ValueError('Invalid Polymarket event slug')
    return value


class PolymarketSettings(BaseModel):
    enabled: bool = False
    events: list[str] = Field(default_factory=list, max_length=8)
    refresh_seconds: int = Field(default=300, ge=60, le=3600)

    @field_validator('events')
    @classmethod
    def normalize_events(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(event_slug(value) for value in values))


def number(value, *, probability: bool = False) -> float | None:
    try:
        result = float(value)
        if not math.isfinite(result) or (probability and not 0 <= result <= 1):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _array(value) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError('Invalid market outcome array')
    return value


def fetch_event(slug: str, timeout: float = 5.0) -> list[dict]:
    from backend.utils.news_context import _read_url, _clean_text

    slug = event_slug(slug)
    payload = json.loads(_read_url(f'https://gamma-api.polymarket.com/events/slug/{slug}', timeout))
    if not isinstance(payload, dict) or not isinstance(payload.get('markets'), list):
        raise ValueError('Polymarket returned an invalid event')
    markets = []
    for market in payload['markets'][:100]:
        outcomes = _array(market.get('outcomes', []))
        prices = _array(market.get('outcomePrices', []))
        if len(outcomes) != len(prices):
            raise ValueError('Polymarket outcome prices do not match outcomes')
        markets.append({
            'id': str(market.get('id', '')),
            'title': _clean_text(market.get('groupItemTitle') or market.get('question'))[:500],
            'question': _clean_text(market.get('question'))[:500],
            'closed': bool(payload.get('closed') or market.get('closed')),
            'active': bool(market.get('active')),
            'outcomes': [{'label': _clean_text(label)[:100], 'probability': number(price, probability=True)} for label, price in zip(outcomes, prices)],
            'best_bid': number(market.get('bestBid'), probability=True),
            'best_ask': number(market.get('bestAsk'), probability=True),
            'last_trade_price': number(market.get('lastTradePrice'), probability=True),
            'change_24h': number(market.get('oneDayPriceChange')),
            'volume_24h': number(market.get('volume24hr')),
            'liquidity': number(market.get('liquidity')),
        })
    if not markets:
        raise ValueError('No markets found for this event')
    return [{
        'slug': slug, 'title': _clean_text(payload.get('title'))[:500],
        'url': f'https://polymarket.com/event/{slug}',
        'closed': bool(payload.get('closed')), 'end_date': payload.get('endDate'),
        'volume_24h': number(payload.get('volume24hr')),
        'liquidity': number(payload.get('liquidity')), 'markets': markets,
    }]


def get_polymarket_context() -> dict:
    from backend.config import config
    from backend.utils.news_context import _fetch_cached, _parse_iso

    settings = PolymarketSettings.model_validate(getattr(config, 'polymarket', {}) or {})
    result = {'enabled': settings.enabled, 'refresh_seconds': settings.refresh_seconds, 'events': [], 'source_health': {}}
    if not settings.enabled or not settings.events:
        return result
    now = datetime.now(timezone.utc)

    def collect(slug):
        return slug, _fetch_cached(
            f'polymarket:{slug}', lambda: fetch_event(slug), now=now,
            ttl=timedelta(seconds=settings.refresh_seconds), stale_ttl=timedelta(hours=1),
        )

    with ThreadPoolExecutor(max_workers=len(settings.events), thread_name_prefix='polymarket') as pool:
        for slug, source in pool.map(collect, settings.events):
            fetched_at = _parse_iso(source.get('fetched_at'))
            if source.get('stale') and (not fetched_at or now - fetched_at > timedelta(hours=1)):
                source = {**source, 'items': [], 'status': 'unavailable', 'stale': False}
            health = {key: value for key, value in source.items() if key != 'items'}
            result['source_health'][f'polymarket:{slug}'] = health
            result['events'].extend({**event, **health} for event in source.get('items', []))
    return result


def intelligence_items(context: dict) -> list[dict]:
    """One bounded headline per event, preserving price changes in digest IDs."""
    from backend.utils.news_context import _make_item, _parse_iso

    items = []
    for event in context.get('events', []):
        quotes = []
        for market in event['markets'][:12]:
            probabilities = ', '.join(f"{o['label']} {o['probability'] * 100:.2f}%" for o in market['outcomes'] if o['probability'] is not None)
            state = 'closed' if market['closed'] else 'open' if market['active'] else 'inactive'
            quotes.append(f"{market['title']} [{state}]: {probabilities or 'price unavailable'}")
        cached = 'STALE cached snapshot; ' if event.get('stale') else ''
        title = f"Polymarket — {event['title']} ({cached}market-implied probabilities, not confirmed facts; fetched {event.get('fetched_at')}): " + '; '.join(quotes)
        items.append(_make_item(source='Polymarket', title=title, category='prediction_market', impact='medium', relevance=0.85, published_at=_parse_iso(event.get('fetched_at')), url=event['url']))
    return items
