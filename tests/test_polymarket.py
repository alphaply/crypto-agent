import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.utils import news_context, polymarket


SLUG = 'fed-decision-in-september-762'


@pytest.fixture
def event_payload():
    return {'title': 'Fed decision', 'markets': [{
        'id': '1', 'question': '25 bps cut?', 'outcomes': '["Yes", "No"]',
        'outcomePrices': '["0", "1"]', 'bestBid': 0, 'bestAsk': 0.01,
        'volume24hr': '1200', 'active': True,
    }]}


def test_url_validation_and_deduplication():
    settings = polymarket.PolymarketSettings(events=[SLUG, f'https://polymarket.com/zh/event/{SLUG}?tid=123'])
    assert settings.events == [SLUG]
    for value in ['http://127.0.0.1/event/a', 'https://polymarket.com.evil.test/event/a', 'https://polymarket.com@evil.test/event/a', '../secret', 'https://polymarket.com/event/a/b']:
        with pytest.raises(ValueError):
            polymarket.event_slug(value)
    with pytest.raises(ValueError):
        polymarket.PolymarketSettings(refresh_seconds=1)


def test_prices_missing_values_and_closed_markets(monkeypatch, event_payload):
    monkeypatch.setattr(news_context, '_read_url', lambda *_: json.dumps(event_payload).encode())
    event = polymarket.fetch_event(SLUG)[0]
    market = event['markets'][0]
    assert market['outcomes'][0]['probability'] == 0
    assert market['best_bid'] == 0
    assert market['liquidity'] is None
    assert market['volume_24h'] == 1200
    event_payload['closed'] = True
    event_payload['markets'][0]['outcomePrices'] = ['NaN', '1.2']
    event = polymarket.fetch_event(SLUG)[0]
    assert event['markets'][0]['closed'] is True
    assert all(item['probability'] is None for item in event['markets'][0]['outcomes'])


def test_mismatched_outcomes_fail(monkeypatch, event_payload):
    event_payload['markets'][0]['outcomePrices'] = '["0.2"]'
    monkeypatch.setattr(news_context, '_read_url', lambda *_: json.dumps(event_payload).encode())
    with pytest.raises(ValueError, match='match'):
        polymarket.fetch_event(SLUG)


def test_cache_failure_isolation_and_disable(monkeypatch, event_payload):
    from backend.config import config

    monkeypatch.setattr(config, 'polymarket', {'enabled': True, 'events': [SLUG, 'missing-event'], 'refresh_seconds': 60})
    monkeypatch.setattr(news_context, '_memory_cache', {})
    monkeypatch.setattr(news_context, '_failure_cache', {})
    monkeypatch.setattr(news_context, '_load_persistent_cache', lambda _: None)
    monkeypatch.setattr(news_context, '_save_persistent_cache', lambda *_: None)
    calls = []

    def read(url, _timeout):
        calls.append(url)
        if 'missing-event' in url:
            raise TimeoutError('offline')
        return json.dumps(event_payload).encode()

    monkeypatch.setattr(news_context, '_read_url', read)
    first = polymarket.get_polymarket_context()
    second = polymarket.get_polymarket_context()
    assert len(calls) == 2
    assert first['events'] == second['events']
    assert first['source_health']['polymarket:missing-event']['status'] == 'unavailable'
    monkeypatch.setattr(config, 'polymarket', {'enabled': False, 'events': [SLUG]})
    assert not polymarket.get_polymarket_context()['events']
    assert len(calls) == 2


def test_stale_cache_and_prompt_preserve_predictions(monkeypatch, event_payload):
    from backend.config import config
    from backend.utils.formatters import format_market_data_to_text

    monkeypatch.setattr(news_context, '_read_url', lambda *_: json.dumps(event_payload).encode())
    event = polymarket.fetch_event(SLUG)[0]
    monkeypatch.setattr(config, 'polymarket', {'enabled': True, 'events': [SLUG]})
    monkeypatch.setattr(news_context, '_memory_cache', {})
    monkeypatch.setattr(news_context, '_failure_cache', {})
    cached_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    monkeypatch.setattr(news_context, '_load_persistent_cache', lambda _: {'fetched_at': cached_at.isoformat(), 'payload': [event]})

    def offline(*_):
        raise TimeoutError('offline')

    monkeypatch.setattr(news_context, '_read_url', offline)
    context = polymarket.get_polymarket_context()
    assert context['events'][0]['stale']
    items = polymarket.intelligence_items(context)
    assert 'STALE' in items[0]['title']
    output = format_market_data_to_text({'news_context': {'headlines': ['RSS'] * 10, 'items': items}})
    assert 'Fed decision' in output
    assert 'Yes 0.00%' in output
    monkeypatch.setattr(news_context, '_failure_cache', {})
    monkeypatch.setattr(news_context, '_load_persistent_cache', lambda _: {'fetched_at': (cached_at - timedelta(hours=2)).isoformat(), 'payload': [event]})
    assert not polymarket.get_polymarket_context()['events']


def test_preview_requires_auth_and_validates_target(monkeypatch, event_payload):
    from backend.app.api.config import router
    from backend.app.core.deps import get_current_user

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/api/config/polymarket/test', json={'event': SLUG}).status_code in {401, 403}
    app.dependency_overrides[get_current_user] = lambda: {'role': 'admin'}
    assert client.post('/api/config/polymarket/test', json={'event': 'https://localhost/event/x'}).status_code == 400
    monkeypatch.setattr(news_context, '_read_url', lambda *_: json.dumps(event_payload).encode())
    response = client.post('/api/config/polymarket/test', json={'event': SLUG})
    assert response.status_code == 200
    assert response.json()['event']['fetched_at']


def test_config_round_trip_and_export(monkeypatch, tmp_path):
    import sqlite3
    from backend import config_store
    from backend.database_schema import initialize_schema

    path = tmp_path / 'config.db'
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    settings = {'enabled': True, 'events': [f'https://polymarket.com/zh/event/{SLUG}'], 'refresh_seconds': 120}
    config_store.save_runtime_snapshot({'polymarket': settings}, [])
    saved = config_store.load_management_snapshot()['globals']['polymarket']
    assert saved == {**settings, 'events': [SLUG]}
    assert config_store.export_full_snapshot(include_secrets=False)['app_settings']['polymarket'] == saved


def test_prediction_only_news_and_threshold_probabilities(monkeypatch, event_payload):
    # Threshold events overlap; do not normalize probabilities across markets.
    event_payload['markets'][0]['outcomePrices'] = '["0.9", "0.1"]'
    event_payload['markets'].append({**event_payload['markets'][0], 'id': '2', 'question': 'BTC above 80k?'})
    monkeypatch.setattr(news_context, '_read_url', lambda *_: json.dumps(event_payload).encode())
    event = polymarket.fetch_event(SLUG)[0]
    assert sum(m['outcomes'][0]['probability'] for m in event['markets']) == 1.8
    event['fetched_at'] = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(polymarket, 'get_polymarket_context', lambda: {'enabled': True, 'events': [event], 'source_health': {'polymarket:test': {'status': 'ok'}}})
    monkeypatch.setattr(news_context, '_fetch_cached', lambda *_args, **_kwargs: {'items': [], 'status': 'unavailable'})
    monkeypatch.setattr(news_context, '_news_digest', lambda *_: '')
    result = news_context.fetch_news_risk_context('BTC/USDT')
    assert result['available']
    assert 'Polymarket' in result['headlines'][0]
    assert 'polymarket:test' in result['source_health']
