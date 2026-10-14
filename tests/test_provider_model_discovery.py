"""Offline supplier catalogs: credentials, protocol contracts and bounded failures."""
import asyncio
import copy
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.config import router
from backend.app.core.deps import get_current_user
from backend.app.services import provider_model_service as service
from backend.utils import provider_catalog as catalog, jev


KEY = 'fixture-discovery-secret-123456'
BASE = 'https://supplier.example/v1'
ENDPOINT = '/api/config/provider-models/discover'


def request_body(**overrides):
    return {'api_base': BASE, 'credential': {'mode': 'draft', 'api_key': KEY}, **overrides}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    # Discovery must never cross into any persistence, reload or generation path.
    from backend import config_store, database_agent_runs, database_usage
    from backend.app.services import config_service, news_service
    from backend.utils import llm_utils
    def forbidden(*args, **kwargs):
        pytest.fail('Discovery attempted a configuration or generation side effect')
    for module, name in [(config_store, 'save_runtime_snapshot'), (config_service, 'save_config_payload'),
                         (config_service.global_config, 'reload_config'), (database_usage, 'start_usage'),
                         (database_agent_runs, 'start_agent_run'), (llm_utils, 'build_chat_model'),
                         (news_service, 'refresh_news')]:
        monkeypatch.setattr(module, name, forbidden)
    with TestClient(app) as value:
        yield value


@pytest.fixture
def transport(monkeypatch):
    calls = []
    def install(handler):
        async def send(request):
            calls.append(request)
            value = handler(request)
            return await value if hasattr(value, '__await__') else value
        monkeypatch.setattr(catalog, '_client', lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(send), follow_redirects=False))
        return calls
    return install


def test_route_has_no_model_precondition_and_never_echoes_keys(client, transport, caplog):
    transport(lambda r: httpx.Response(200, json={'data': [{'id': 'real-Model'}]}))
    response = client.post(ENDPOINT, json=request_body())
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['models'][0]['id'] == 'real-Model'
    assert KEY not in response.text + caplog.text
    for bad in [request_body(model='unwanted'), request_body(credential={'mode': 'draft', 'api_key': {'secret': KEY}}),
                request_body(credential={'mode': 'saved', 'provider_id': 'x', 'api_key': KEY}),
                request_body(api_protocol=KEY)]:
        rejected = client.post(ENDPOINT, json=bad)
        assert rejected.status_code == 422 and KEY not in rejected.text
        assert rejected.headers['cache-control'] == 'no-store'
    client.app.dependency_overrides.clear()
    denied = client.post(ENDPOINT, json=request_body())
    assert denied.status_code == 401


def test_credentials_are_explicit_and_bound_to_saved_connection(client, transport, monkeypatch):
    saved = {'provider_id': 'p', 'api_base': BASE, 'api_key': KEY, 'model': 'existing'}
    snapshot = {'llm_providers': [saved]}
    original = copy.deepcopy(snapshot)
    monkeypatch.setattr(service, 'load_effective_runtime_snapshot', lambda: snapshot)
    calls = transport(lambda r: httpx.Response(200, json={'data': []}))
    def discover(**kwargs):
        return client.post(ENDPOINT, json=request_body(credential={'mode': 'saved', 'provider_id': 'p'}, **kwargs))
    assert discover(api_base=' HTTPS://SUPPLIER.EXAMPLE/v1/ ').status_code == 200
    assert calls[-1].headers['authorization'] == 'Bearer ' + KEY
    for changed in [{'api_base': BASE + '/Other'}, {'compatibility_mode': 'openai'}, {'api_protocol': 'decisions'}]:
        result = discover(**changed)
        assert result.status_code == 400 and result.json()['detail']['code'] == 'connection_changed'
    for credential in [{'mode': 'none'}, {'mode': 'draft', 'api_key': ''}, {'mode': 'saved', 'provider_id': 'copy'}]:
        response = client.post(ENDPOINT, json=request_body(credential=credential))
        assert response.status_code in {400, 404}
    assert len(calls) == 1
    assert client.post(ENDPOINT, json=request_body(api_base='https://new.example', credential={'mode': 'draft', 'api_key': 'replacement-secret'})).status_code == 200
    assert calls[-1].headers['authorization'] == 'Bearer replacement-secret'
    assert snapshot == original
    saved['api_key'] = ''
    assert discover().json()['detail']['code'] == 'missing_credentials'
    snapshot['llm_providers'] = []
    assert discover().status_code == 404


@pytest.mark.parametrize('encoded', [False, True])
@pytest.mark.parametrize('credential_mode', ['draft', 'saved'])
def test_credential_in_source_path_is_rejected_without_echo_or_supplier_request(client, transport, monkeypatch, caplog, encoded, credential_mode):
    path_key = ''.join(f'%{ord(char):02X}' for char in KEY) if encoded else KEY
    base = 'https://supplier.example/' + path_key + '/v1'
    calls = transport(lambda request: pytest.fail('A credential-bearing source must not reach the supplier'))
    if credential_mode == 'saved':
        snapshot = {'llm_providers': [{'provider_id': 'stored', 'api_base': base, 'api_key': KEY, 'model': 'old-alias'}]}
        original = copy.deepcopy(snapshot)
        monkeypatch.setattr(service, 'load_effective_runtime_snapshot', lambda: snapshot)
        credential = {'mode': 'saved', 'provider_id': 'stored'}
    else:
        credential = {'mode': 'draft', 'api_key': KEY}
    response = client.post(ENDPOINT, json=request_body(api_base=base, credential=credential))
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'invalid_connection'
    assert response.headers['cache-control'] == 'no-store'
    assert KEY not in response.text + caplog.text and path_key not in response.text
    assert calls == []
    if credential_mode == 'saved':
        assert snapshot == original


def test_shared_catalog_adapter_rejects_credential_path_for_legacy_jev(transport, caplog):
    calls = transport(lambda request: pytest.fail('Legacy adapter must not send a credential in the URL'))
    with pytest.raises(catalog.CatalogError) as failure:
        asyncio.run(catalog.fetch_models({'api_base': 'https://supplier.example/' + KEY,
                                         'api_key': KEY, 'api_protocol': 'decisions', 'decisions_api': 'bai'}))
    assert failure.value.code == 'invalid_connection' and failure.value.status == 400
    assert KEY not in str(failure.value) + str(failure.value.detail) + caplog.text
    assert calls == []


@pytest.mark.parametrize('base,mode,url,protocol', [
    ('https://api.deepseek.com', 'auto', 'https://api.deepseek.com/models', 'deepseek'),
    (BASE + '/', 'openai', BASE + '/models', 'openai'),
    ('https://proxy.example/Custom/V2', 'auto', 'https://proxy.example/Custom/V2/models', 'openai'),
    ('https://api.anthropic.com', 'auto', 'https://api.anthropic.com/v1/models', 'anthropic'),
    ('https://proxy.example/Custom/v1/', 'anthropic', 'https://proxy.example/Custom/v1/models', 'anthropic'),
    ('http://127.0.0.1:8080/v1', 'auto', 'http://127.0.0.1:8080/v1/models', 'openai'),
    ('https://api.b.ai/v1', 'openai', 'https://api.b.ai/v1/models', 'openai'),
])
def test_protocol_urls_headers_raw_ids_and_families(client, transport, base, mode, url, protocol):
    calls = transport(lambda r: httpx.Response(200, json={'data': [
        {'id': 'Z/Model', 'display_name': 'Friendly name'}, {'id': 'claude-custom'},
        {'id': 'gpt-custom'}, {'id': 'Z/Model'}, {'id': 'z/model'}, None]}))
    response = client.post(ENDPOINT, json=request_body(api_base=base, compatibility_mode=mode))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['source']['protocol'] == protocol and str(calls[0].url) == url
    assert [row['id'] for row in data['models']] == ['Z/Model', 'claude-custom', 'gpt-custom', 'z/model']
    assert data['models'][0]['name'] == 'Friendly name'
    headers = calls[0].headers
    if protocol == 'anthropic':
        assert headers['x-api-key'] == KEY and headers['anthropic-version'] == '2023-06-01'
        assert 'authorization' not in headers
    else:
        assert headers['authorization'] == 'Bearer ' + KEY and 'x-api-key' not in headers
    assert headers['user-agent']
    if base == 'https://api.b.ai/v1':
        from backend.utils.llm_utils import _provider_default_headers
        assert headers['user-agent'] == _provider_default_headers(base)['User-Agent']


@pytest.mark.parametrize('base', ['file:///tmp/a', 'https://user:secret@example.com', BASE+'?key='+KEY,
                                BASE+'#fragment', 'https://bad:port', BASE+'\n', BASE+'\\other'])
def test_invalid_connection_never_reaches_network(client, transport, base):
    calls = transport(lambda r: pytest.fail('invalid URL sent'))
    assert client.post(ENDPOINT, json=request_body(api_base=base)).status_code == 400
    assert calls == []


@pytest.mark.parametrize('status,code', [(401, 'credentials_rejected'), (403, 'credentials_rejected'),
    (404, 'catalog_not_supported'), (405, 'catalog_not_supported'), (429, 'rate_limited'),
    (302, 'upstream_error'), (503, 'upstream_error')])
def test_upstream_failures_are_safe_and_never_retried(client, transport, caplog, status, code):
    calls = transport(lambda r: httpx.Response(status, text=KEY, headers={'Location': 'https://other.example/'+KEY}))
    response = client.post(ENDPOINT, json=request_body())
    assert response.status_code == 502 and response.json()['detail']['code'] == code
    assert len(calls) == 1 and KEY not in response.text + caplog.text


@pytest.mark.parametrize('payload,code', [({'data': []}, None), ({'data': [None, {}, {'id': ''}]}, 'invalid_response'),
    ({'data': [{'id': KEY}]}, 'invalid_response'), ({'wrong': []}, 'invalid_response'), ([], 'invalid_response')])
def test_empty_and_invalid_catalogs(client, transport, payload, code):
    transport(lambda r: httpx.Response(200, json=payload))
    response = client.post(ENDPOINT, json=request_body())
    if code:
        assert response.status_code == 502 and response.json()['detail']['code'] == code
    else:
        assert response.json()['models'] == [] and response.json()['complete']


def test_bad_json_size_limit_and_total_deadline(client, transport, monkeypatch):
    transport(lambda r: httpx.Response(200, content=b'not-json'))
    assert client.post(ENDPOINT, json=request_body()).json()['detail']['code'] == 'invalid_response'
    monkeypatch.setattr(catalog, 'MAX_PAGE_BYTES', 32)
    transport(lambda r: httpx.Response(200, content=b'x'*33))
    assert client.post(ENDPOINT, json=request_body()).json()['detail']['code'] == 'response_too_large'
    monkeypatch.setattr(catalog, 'TOTAL_TIMEOUT', .01)
    async def slow(request):
        await asyncio.sleep(.1)
        return httpx.Response(200, json={'data': []})
    calls = transport(slow)
    assert client.post(ENDPOINT, json=request_body()).status_code == 504
    assert len(calls) == 3


def test_anthropic_pagination_and_partial_results(client, transport, monkeypatch):
    def pages(request):
        after = request.url.params.get('after_id')
        return httpx.Response(200, json={'data': [{'id': 'Second' if after else 'First'}],
            'has_more': not after, 'last_id': 'First'})
    calls = transport(pages)
    body = request_body(compatibility_mode='anthropic')
    result = client.post(ENDPOINT, json=body).json()
    assert result['complete'] and len(result['models']) == 2
    assert calls[1].url.params['after_id'] == 'First'
    for response, code in [(httpx.Response(503, text=KEY), 'upstream_error'),
            (httpx.Response(200, json={'data': [{'id': 'First'}], 'has_more': True, 'last_id': 'First'}), 'pagination_error')]:
        transport(lambda r: response if r.url.params.get('after_id') else pages(r))
        result = client.post(ENDPOINT, json=body).json()
        assert result['success'] and not result['complete'] and result['warning']['code'] == code
        assert [row['id'] for row in result['models']] == ['First']
    monkeypatch.setattr(catalog, 'MAX_PAGES', 1)
    transport(pages)
    assert client.post(ENDPOINT, json=body).json()['warning']['code'] == 'catalog_limit'


def test_limits_and_unsupported_missing_cursors_are_partial(client, transport, monkeypatch):
    for extra, mode, code in [({'has_more': True}, 'anthropic', 'pagination_error'),
                              ({'next_cursor': 'x'}, 'auto', 'pagination_unsupported'),
                              ({'has_more': True}, 'auto', 'pagination_unsupported')]:
        transport(lambda r: httpx.Response(200, json={'data': [{'id': 'm'}], **extra}))
        data = client.post(ENDPOINT, json=request_body(compatibility_mode=mode)).json()
        assert not data['complete'] and data['warning']['code'] == code
    monkeypatch.setattr(catalog, 'MAX_MODELS', 1)
    transport(lambda r: httpx.Response(200, json={'data': [{'id': 'a'}, {'id': 'b'}]}))
    data = client.post(ENDPOINT, json=request_body()).json()
    assert len(data['models']) == 1 and data['warning']['code'] == 'catalog_limit'


@pytest.mark.parametrize('array_key', ['models', 'data'])
def test_jev_draft_without_model_and_legacy_same_candidates(client, transport, array_key):
    calls = transport(lambda r: httpx.Response(200, json={array_key: [
        {'name': 'jev-latest'}, {'id': 'jev-1.13.0'}, {'name': 'other-family'}, {'name': 'jev-latest'}]}))
    body = request_body(api_base='https://api.typesafe.ai', api_protocol='decisions', decisions_api='typesafe')
    data = client.post(ENDPOINT, json=body).json()
    provider = {**body, 'api_key': KEY, 'model': ''}
    assert data['models'] == jev.list_models(provider)
    assert [row['id'] for row in data['models']] == ['jev-1.13.0', 'jev-latest']
    assert all(str(r.url) == 'https://api.typesafe.ai/v1/models' and r.method == 'GET' for r in calls)
    with pytest.raises(jev.JevError, match='model identifier'):
        jev.score_news({}, provider)


def test_jev_other_family_is_a_valid_empty_catalog(client, transport):
    transport(lambda r: httpx.Response(200, json={'data': [{'id': 'chat-only'}]}))
    data = client.post(ENDPOINT, json=request_body(api_protocol='decisions')).json()
    assert data['models'] == [] and data['complete']


def test_secret_cursor_is_not_sent_or_logged(client, transport, caplog):
    calls = transport(lambda r: httpx.Response(200, json={
        'data': [{'id': 'first'}], 'has_more': True, 'last_id': KEY}))
    response = client.post(ENDPOINT, json=request_body(compatibility_mode='anthropic'))
    assert response.status_code == 200 and not response.json()['complete']
    assert len(calls) == 1 and KEY not in response.text + caplog.text


def test_later_timeout_preserves_partial_catalog(client, transport, monkeypatch):
    monkeypatch.setattr(catalog, 'TOTAL_TIMEOUT', .02)
    async def delayed(request):
        if request.url.params.get('after_id'):
            await asyncio.sleep(.2)
        return httpx.Response(200, json={'data': [{'id': 'first'}], 'has_more': True, 'last_id': 'first'})
    calls = transport(delayed)
    response = client.post(ENDPOINT, json=request_body(compatibility_mode='anthropic'))
    assert response.status_code == 200
    assert response.json()['warning']['code'] == 'timeout' and len(calls) == 2


def test_saved_and_replaced_keys_do_not_change_encrypted_config(tmp_path, monkeypatch, transport):
    from backend import config_store
    from backend.config import Config
    from backend.database_schema import initialize_schema
    path = tmp_path / 'discovery-config.sqlite'
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    monkeypatch.setattr(config_store, 'DB_NAME', path)
    monkeypatch.setattr(config_store, 'load_dotenv', lambda *a, **k: None)
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'discovery-test-encryption')
    config_store.save_runtime_snapshot({'enable_scheduler': False}, [], [{
        'provider_id': 'stored', 'name': 'Stored fixture', 'api_base': BASE, 'api_key': KEY,
        'model': 'my-private-model', 'input_price_per_m': 1.25, 'compatibility_mode': 'auto',
        'thinking_enabled': True, 'reasoning_effort': 'medium'}], [])
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only discovery attempted a write or reload')
    monkeypatch.setattr(config_store, 'save_runtime_snapshot', forbidden)
    monkeypatch.setattr(Config, 'reload_config', forbidden)
    calls = transport(lambda r: httpx.Response(200, json={'data': []}))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'admin'}
    with TestClient(app) as client:
        for credential, status in [({'mode': 'saved', 'provider_id': 'stored'}, 200),
                ({'mode': 'draft', 'api_key': 'replacement-draft-key'}, 200), ({'mode': 'none'}, 400)]:
            response = client.post(ENDPOINT, json=request_body(credential=credential))
            assert response.status_code == status, response.text
    assert [r.headers['authorization'] for r in calls] == ['Bearer ' + KEY, 'Bearer replacement-draft-key']
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before
