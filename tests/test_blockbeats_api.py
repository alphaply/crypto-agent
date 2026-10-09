import json
from datetime import datetime, timezone

import httpx
import pytest

from backend import config_store, database
from backend.app.schemas.news import BLOCKBEATS_API_URL, BLOCKBEATS_LEGACY_RSS_URL, NewsSettings, NewsSource
from backend.app.schemas.payloads import ConfigGlobalPayload
from backend.app.services import news_service
from backend.database_schema import initialize_schema
from backend.utils import blockbeats, news_context


NOW = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)


def newsflash(identifier=123, **fields):
    return {'id': identifier, 'title': '机构公布资金流向', 'content': '<p>完整中文正文 &amp; 数据。</p>',
            'create_time': '2026-10-09 11:30:00', 'link': f'https://www.theblockbeats.info/flash/{identifier}', **fields}


def api_response(rows, **fields):
    return httpx.Response(200, json={'status': 0, 'data': {'page': 1, 'data': rows}, **fields})


def test_json_api_uses_header_fixed_endpoint_and_stable_page_size(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        page = kwargs['params']['page']
        return api_response([newsflash(i) for i in range((page-1)*50+1, page*50+1)])

    monkeypatch.setattr(blockbeats.httpx, 'get', get)
    items = blockbeats.fetch_newsflash('private-test-key', limit=80, now=NOW)
    assert len(items) == 80 and len({item['guid'] for item in items}) == 80
    assert [call[1]['params'] for call in calls] == [
        {'page': 1, 'size': 50, 'lang': 'cn'}, {'page': 2, 'size': 50, 'lang': 'cn'}]
    assert all(url == BLOCKBEATS_API_URL for url, _ in calls)
    assert calls[0][1]['headers']['api-key'] == 'private-test-key'
    assert calls[0][1]['follow_redirects'] is False
    assert items[0]['content'] == '完整中文正文 & 数据。'
    assert items[0]['published_at'] == '2026-10-09T03:30:00+00:00'
    assert 'private-test-key' not in json.dumps(items)


def test_time_window_deduplication_and_bad_records(monkeypatch):
    monkeypatch.setattr(blockbeats.httpx, 'get', lambda *_a, **_kw: api_response([
        newsflash(), newsflash(), newsflash(124, create_time='2026-10-07 11:00:00'),
        newsflash(125, create_time='2026-10-10 11:00:00'), newsflash(126, create_time='invalid'),
        newsflash(127, link='javascript:alert(1)'),
        newsflash(128, link='https://[malformed'),
    ]))
    items = blockbeats.fetch_newsflash('key', now=NOW)
    assert [item['id'] for item in items] == ['blockbeats:123', 'blockbeats:127', 'blockbeats:128']
    assert items[1]['url'] == 'https://www.theblockbeats.info/flash/127'
    assert items[2]['url'] == 'https://www.theblockbeats.info/flash/128'


@pytest.mark.parametrize('response', [
    httpx.Response(401, text='private-test-key'),
    httpx.Response(429, text='private-test-key'),
    httpx.Response(302, headers={'Location': 'https://example.com/stolen'}),
    api_response([], status=101, message='private-test-key'),
    httpx.Response(200, text='private-test-key'),
    api_response([], data=[]),
])
def test_failed_api_never_exposes_provider_body_or_follows_redirect(monkeypatch, response):
    monkeypatch.setattr(blockbeats.httpx, 'get', lambda *_a, **_kw: response)
    with pytest.raises(blockbeats.BlockBeatsError) as caught:
        blockbeats.fetch_newsflash('private-test-key', now=NOW)
    assert 'private-test-key' not in str(caught.value)


def test_missing_key_is_configuration_required_and_never_fetches(monkeypatch):
    monkeypatch.setattr(blockbeats.httpx, 'get', lambda *_a, **_kw: pytest.fail('must not fetch without a key'))
    source = NewsSettings().sources[0]
    result = news_service._source_result(source, NewsSettings(), NOW)
    assert result['status'] == 'configuration_required' and result['items'] == []
    assert 'API Key' in result['error']


def test_defaults_and_legacy_source_migration_preserve_switches():
    assert NewsSettings().sources[0].kind == 'blockbeats'
    migrated = NewsSource.model_validate({'id': 'blockbeats', 'name': '律动自定义名称', 'kind': 'rss',
                                         'url': BLOCKBEATS_LEGACY_RSS_URL, 'enabled': False, 'language': 'en'})
    assert migrated.kind == 'blockbeats' and migrated.url == BLOCKBEATS_API_URL
    assert not migrated.enabled and migrated.language == 'en' and migrated.name == '律动自定义名称'
    custom = NewsSource(id='custom', name='Custom', kind='rss', url='https://example.com/rss')
    assert custom.kind == 'rss'
    with pytest.raises(ValueError):
        NewsSource(id='blockbeats', name='律动', kind='blockbeats', url='https://example.com/api')


@pytest.fixture
def config_db(tmp_path, monkeypatch):
    path = tmp_path / 'config.sqlite'
    monkeypatch.setattr(database, 'DB_NAME', str(path))
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *_a, **_kw: None)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'blockbeats-test-master')
    with database.get_db_conn() as conn:
        initialize_schema(conn)
    news_context._memory_cache.clear()
    news_context._failure_cache.clear()


def test_blockbeats_key_uses_encrypted_secret_store_and_config_roundtrip(config_db):
    payload = ConfigGlobalPayload.model_validate({'secrets': {'global_blockbeats_api_key': {'value': 'private-test-key'}}})
    config_store.save_runtime_snapshot(payload.model_dump(), [])
    assert config_store.load_runtime_snapshot()['global_blockbeats_api_key'] == 'private-test-key'
    snapshot = config_store.load_management_snapshot()
    assert snapshot['globals']['secrets']['global_blockbeats_api_key']['configured']
    assert 'private-test-key' not in json.dumps(snapshot)
    with database.get_db_conn() as conn:
        assert 'private-test-key' not in '\n'.join(conn.iterdump())
    editable = config_store.load_management_snapshot(include_secrets=True)
    assert editable['globals']['secrets']['global_blockbeats_api_key']['value'] == 'private-test-key'
    config_store.save_runtime_snapshot(snapshot['globals'], [])
    assert config_store.load_runtime_snapshot()['global_blockbeats_api_key'] == 'private-test-key'
    snapshot['globals']['secrets']['global_blockbeats_api_key'] = {'clear': True}
    config_store.save_runtime_snapshot(snapshot['globals'], [])
    assert not config_store.load_runtime_snapshot().get('global_blockbeats_api_key')


def test_existing_saved_rss_migrates_on_management_read(config_db):
    legacy = {'sources': [{'id': 'blockbeats', 'name': '律动', 'kind': 'rss',
                          'url': BLOCKBEATS_LEGACY_RSS_URL, 'enabled': False}]}
    config_store.save_runtime_snapshot({}, [])
    with database.get_db_conn() as conn:
        conn.execute("UPDATE app_settings SET value_json=? WHERE key='news'", (json.dumps(legacy),))
        conn.commit()
    source = config_store.load_management_snapshot()['globals']['news']['sources'][0]
    assert source['kind'] == 'blockbeats' and source['enabled'] is False


def test_api_failure_preserves_source_cache_without_storing_credentials(config_db, monkeypatch):
    source = NewsSettings().sources[0]
    monkeypatch.setattr(blockbeats.httpx, 'get', lambda *_a, **_kw: api_response([newsflash()]))
    first = news_service._source_result(source, NewsSettings(), NOW, 'private-test-key')
    assert first['status'] == 'ok'
    monkeypatch.setattr(blockbeats.httpx, 'get', lambda *_a, **_kw: httpx.Response(401, text='private-test-key'))
    later = NOW.replace(minute=1)
    failed = news_service._source_result(source, NewsSettings(), later, 'private-test-key')
    assert failed['status'] == 'stale' and failed['items'] == first['items']
    with database.get_db_conn() as conn:
        assert 'private-test-key' not in '\n'.join(conn.iterdump())
