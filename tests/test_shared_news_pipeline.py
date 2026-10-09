import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database, database_news
from backend.app.api.news import router
from backend.app.core.deps import get_current_user
from backend.app.schemas.news import NewsSettings, NewsSource
from backend.app.services import news_service, pricing_service
from backend.database_schema import initialize_schema
from backend.utils import news_context, jev, binance_announcements


@pytest.fixture
def news_env(tmp_path, monkeypatch):
    from backend.utils import llm_utils
    monkeypatch.setattr(llm_utils, 'sync_langsmith_environment', lambda: {})
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'news.sqlite'))
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    snapshot = {'news': {'scorer_provider_id': 'jev', 'summarizer_provider_id': 'summary'}, 'llm_providers': [
        {'provider_id': 'jev', 'model': 'jev-1.13.0', 'api_protocol': 'decisions', 'api_base': 'https://api.b.ai/v1', 'api_key': 'test-secret', 'input_price_per_m': 0.042, 'output_price_per_m': 0},
        {'provider_id': 'summary', 'model': 'test-summary', 'api_key': 'test-secret', 'input_price_per_m': 1, 'output_price_per_m': 2},
    ]}
    monkeypatch.setattr(news_service, '_runtime', lambda: snapshot)
    monkeypatch.setattr(pricing_service, '_runtime', lambda: snapshot)
    news_context._memory_cache.clear()
    news_context._failure_cache.clear()
    return snapshot


def response(score=3, levels=5):
    probabilities = {str(i): float(i == score) for i in range(levels)}
    return {'model': 'jev-1.13.0', 'answers': {'market_relevance': {'type': 'score', 'score': score, 'confidence': 1, 'probabilities': probabilities, 'legend': {str(i): f'Level {i}' for i in range(levels)}}}, 'usage': {'input_tokens': 100, 'output_tokens': 20}}


def article(title='律动：机构增持比特币', suffix='a'):
    return {'id': suffix, 'source': 'BlockBeats', 'title': title, 'content': '机构公布新增资金流入。', 'published_at': datetime.now(timezone.utc).isoformat(), 'url': f'https://example.com/{suffix}', 'category': 'crypto'}


def test_disabled_pipeline_does_not_feed_old_snapshot(news_env):
    owner = database_news.acquire_lease('news', seconds=60)
    database_news.publish_snapshot({'available': True, 'digest': 'old', 'as_of': database_news.now_iso()}, owner)
    news_env['news']['enabled'] = False
    assert news_service.get_latest_news()['available'] is False
    assert news_service.get_latest_news()['status'] == 'disabled'


def test_provider_failure_diagnostic_cannot_expose_response_or_credentials(news_env, monkeypatch):
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *_: ([article()], {'test': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, 'score_news', lambda *_a, **_kw: {'score': 90})
    monkeypatch.setattr(news_service, '_summarize', lambda *_: (_ for _ in ()).throw(ValueError('provider-error-with-test-secret')))
    news_service.refresh_news(background=False)
    assert 'test-secret' not in json.dumps(news_service.get_news_status())
    assert 'test-secret' not in json.dumps(news_service.get_latest_news())


def test_public_predictions_read_only_published_shared_snapshot(news_env, monkeypatch):
    from backend.app.api.public import polymarket as read_predictions
    from backend.utils import polymarket
    monkeypatch.setattr(polymarket, 'get_polymarket_context', lambda: pytest.fail('public read must not collect'))
    assert read_predictions()['events'] == []
    owner = database_news.acquire_lease('news', seconds=60)
    database_news.publish_snapshot({'available': True, 'as_of': database_news.now_iso(),
                                    'polymarket': {'enabled': True, 'events': [{'slug': 'published'}]}}, owner)
    assert read_predictions()['events'] == [{'slug': 'published'}]


def test_jev_uses_decisions_api_and_caches_real_usage(news_env, monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=response(), request=httpx.Request('POST', url))
    monkeypatch.setattr(jev.httpx, 'post', post)
    provider = news_env['llm_providers'][0]
    first = jev.score_news(article(), provider)
    second = jev.score_news(article(), provider)
    assert first['score'] == 75 and second['cached'] is True
    assert second['raw_score'] == 3 and second['level_count'] == 5
    assert second['legend'] == response()['answers']['market_relevance']['legend']
    assert len(calls) == 1
    assert calls[0][0] == 'https://api.b.ai/v1/decisions'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer test-secret'
    assert 'messages' not in calls[0][1]['json']
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM durable_usage').fetchone()
    assert row['purpose'] == 'news_score' and row['provider_id'] == 'jev'
    assert row['prompt_tokens'] == 100 and row['completion_tokens'] == 20
    assert row['cost'] == pytest.approx(0.0000042)


def test_rubric_change_invalidates_cache_and_maps_levels(news_env, monkeypatch):
    calls = []
    def post(url, **kwargs):
        criteria = kwargs['json']['questions']['market_relevance']['criteria']
        calls.append(criteria)
        return httpx.Response(200, json=response(score=len(criteria)-1, levels=len(criteria)), request=httpx.Request('POST', url))
    monkeypatch.setattr(jev.httpx, 'post', post)
    provider = news_env['llm_providers'][0]
    assert jev.score_news(article(), provider)['score'] == 100
    assert jev.score_news(article(), provider, criteria=['irrelevant', 'relevant'])['score'] == 100
    assert len(calls) == 2


@pytest.mark.parametrize('change', [
    lambda p: p['answers']['market_relevance'].update(score=5),
    lambda p: p['answers']['market_relevance']['probabilities'].update({'3': 0.5}),
    lambda p: p['answers']['market_relevance'].update(type='choice'),
    lambda p: p.update(answers={}),
    lambda p: p['usage'].update(input_tokens=-1),
])
def test_malformed_decisions_are_rejected(change):
    payload = response()
    change(payload)
    with pytest.raises(jev.JevError):
        jev.parse_response(payload)


def test_jev_versions_are_editable_but_generative_models_and_protocol_are_rejected():
    for model in ('jev-1.13.0', 'jev-latest', 'jev-2.0.0-preview'):
        jev.validate_provider({'model': model, 'api_protocol': 'decisions', 'api_key': 'test-key'})
    for provider in (
        {'model': 'gpt-example', 'api_protocol': 'decisions', 'api_key': 'test-key'},
        {'model': 'jev-2.0', 'api_protocol': 'chat', 'api_key': 'test-key'},
        {'model': 'jev-../../other', 'api_protocol': 'decisions', 'api_key': 'test-key'},
    ):
        with pytest.raises(jev.JevError):
            jev.validate_provider(provider)


def test_hourly_shared_snapshot_reads_never_fetch_or_score(news_env, monkeypatch):
    counts = {'collect': 0, 'score': 0, 'summary': 0}
    def collect(*args):
        counts['collect'] += 1
        return [article()], {'blockbeats': {'status': 'ok'}}, {'enabled': False}
    def score(*args, **kwargs):
        counts['score'] += 1
        return {'score': 90, 'confidence': .8}
    def summarize(*args):
        counts['summary'] += 1
        return '共享市场摘要 [1]'
    monkeypatch.setattr(news_service, 'collect_candidates', collect)
    monkeypatch.setattr(news_service, 'score_news', score)
    monkeypatch.setattr(news_service, '_summarize', summarize)
    assert news_service.refresh_news(background=False)['started']
    first = news_context.fetch_news_risk_context('BTC/USDT')
    second = news_context.fetch_news_risk_context('ETH/USDT')
    assert first['snapshot_id'] == second['snapshot_id']
    assert first['digest'] == '共享市场摘要 [1]'
    assert not news_service.refresh_news(force=False, background=False)['started']
    assert counts == {'collect': 1, 'score': 1, 'summary': 1}
    assert 'test-secret' not in json.dumps(first)


def test_failure_retains_last_good_snapshot(news_env, monkeypatch):
    owner = database_news.acquire_lease('news')
    database_news.publish_snapshot({'available': True, 'digest': 'previous', 'items': [], 'as_of': database_news.now_iso()}, owner)
    database_news.update_state('news', {'status': 'success'}, owner=owner, release=True)
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'rss': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, 'score_news', lambda *a, **kw: (_ for _ in ()).throw(ValueError('offline')))
    news_service.refresh_news(background=False)
    latest = news_service.get_latest_news()
    assert latest['digest'] == 'previous' and latest['stale']
    assert database_news.read_state('news')['status'] == 'error'


def test_empty_filter_publishes_no_invented_summary(news_env, monkeypatch):
    monkeypatch.setattr(news_service, 'collect_candidates', lambda *a: ([article()], {'rss': {'status': 'ok'}}, {}))
    monkeypatch.setattr(news_service, 'score_news', lambda *a, **kw: {'score': 25, 'confidence': 1})
    news_service.refresh_news(background=False)
    assert news_service.get_latest_news()['items'] == []
    assert '阈值' in news_service.get_latest_news()['digest']


def test_lease_prevents_duplicate_refresh_and_old_owner_publish(news_env):
    owner = database_news.acquire_lease('news')
    assert not news_service.refresh_news(background=False)['started']
    assert not database_news.publish_snapshot({'digest': 'bad'}, 'other-owner')
    assert database_news.read_state('news')['running']
    database_news.update_state('news', {}, owner=owner, release=True)


def test_rss_preserves_chinese_content_and_metadata():
    xml = '<rss><channel><item><title>监管新政策</title><description><![CDATA[<p>政策详细内容</p>]]></description><guid>id-1</guid><link>https://example.com/a</link></item></channel></rss>'
    item = news_context._parse_feed(xml.encode(), '律动', 'crypto')[0]
    assert item['content'] == '政策详细内容' and item['guid'] == 'id-1'


def test_news_api_public_read_authenticated_refresh(news_env, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/api/news').status_code == 200
    assert client.post('/api/news/refresh').status_code == 401
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    monkeypatch.setattr('backend.app.api.news.refresh_news', lambda: {'started': True})
    assert client.post('/api/news/refresh').json()['started']


def test_binance_signature_and_payload():
    url = binance_announcements.signed_url('test-secret', timestamp=12345, nonce='abc')
    query, signature = urlsplit(url).query.rsplit('&signature=', 1)
    assert signature == hmac.new(b'test-secret', query.encode(), hashlib.sha256).hexdigest()
    assert parse_qs(query)['topic'] == ['com_announcement_en']
    item = binance_announcements.parse_announcement(json.dumps({'type': 'DATA', 'topic': 'com_announcement_en', 'data': json.dumps({'title': 'Listing notice', 'body': '<p>Details</p>', 'publishDate': 1753257631403, 'catalogId': 48})}))
    assert item['content'] == 'Details' and item['catalog_id'] == 48


def test_binance_missing_credentials_does_not_start_socket(news_env, monkeypatch):
    monkeypatch.setattr(binance_announcements, '_worker', None)
    monkeypatch.setattr(binance_announcements, '_stop', None)
    binance_announcements.ensure_worker(NewsSettings(), news_env)
    assert database_news.read_state('binance-stream')['status'] == 'configuration_required'


def test_custom_sources_validate_and_keep_full_defaults():
    defaults = NewsSettings()
    assert {'blockbeats', 'binance', 'fed_monetary', 'sec', 'treasury', 'fomc'}.issubset({s.id for s in defaults.sources})
    with pytest.raises(ValueError):
        NewsSettings(sources=[NewsSource(id='a', name='A', kind='binance')] * 2)
    with pytest.raises(ValueError):
        NewsSource(id='bad', name='Bad', url='https://user:secret@example.com/feed')
