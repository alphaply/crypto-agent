import base64
import hashlib
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.app.core import security
from backend.mcp import connection, store
from backend.mcp.server import install_mcp, mcp_lifespan


ORIGIN = 'https://crypto.example.test'
CALLBACK = 'https://chatgpt.com/connector/oauth/test-callback'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'oauth.db'))
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'connection-test-master')
    monkeypatch.setattr(security, 'authenticate_password', lambda password: password == 'test-password')
    store.put('settings', 'global', {'enabled': True, 'public_url': ORIGIN})

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_lifespan():
            yield

    app = FastAPI(lifespan=lifespan)
    install_mcp(app)
    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def admin_headers():
    return {'Authorization': 'Bearer ' + security.create_access_token()}


def test_discovery_aliases_have_consistent_issuer_and_never_cache_old_origins(client):
    challenge = client.post('/mcp', json={})
    assert challenge.status_code == 401
    assert f'resource_metadata="{ORIGIN}/oauth/resource-metadata"' in challenge.headers['www-authenticate']
    for path in ('/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-protected-resource', '/oauth/resource-metadata'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['resource'] == ORIGIN + '/mcp'
        assert response.json()['authorization_servers'] == [ORIGIN + '/oauth']
    for path in ('/.well-known/oauth-authorization-server/oauth', '/.well-known/oauth-authorization-server', '/oauth/.well-known/oauth-authorization-server'):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['issuer'] == ORIGIN + '/oauth'
        assert response.json()['registration_endpoint'] == ORIGIN + '/oauth/register'
        assert response.json()['token_endpoint_auth_methods_supported'] == ['none', 'client_secret_post', 'client_secret_basic']
        assert response.json()['code_challenge_methods_supported'] == ['S256']


def test_custom_client_requires_admin_and_registers_without_granting_access(client):
    payload = {'redirect_uri': CALLBACK}
    assert client.post('/api/mcp/oauth-clients', json=payload).status_code == 401
    assert client.get('/api/mcp/connection-check').status_code == 401
    response = client.post('/api/mcp/oauth-clients', json=payload, headers=admin_headers())
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    client_info = response.json()['client']
    assert client_info['scope'] == 'read'
    assert client_info['token_endpoint_auth_method'] == 'client_secret_post'
    assert not store.list_records('access') and not store.list_records('refresh')
    persisted = store.get('client', client_info['client_id'])
    assert persisted['client_secret'] != client_info['client_secret']
    overview = client.get('/api/mcp', headers=admin_headers()).json()
    assert client_info['client_secret'] not in json.dumps(overview)


@pytest.mark.parametrize('uri', ['https://example.com/callback', 'https://chatgpt.com.evil.test/callback',
                                'http://chatgpt.com/callback', 'https://user@chatgpt.com/callback',
                                'https://chatgpt.com/callback#fragment', 'https://chatgpt.com/'])
def test_custom_client_rejects_wrong_callback_hosts(client, uri):
    assert client.post('/api/mcp/oauth-clients', json={'redirect_uri': uri}, headers=admin_headers()).status_code == 422


def test_custom_client_blocks_unapplied_public_url(client):
    store.put('settings', 'global', {'enabled': True, 'public_url': 'https://other.example.test'})
    response = client.post('/api/mcp/oauth-clients', json={'redirect_uri': CALLBACK}, headers=admin_headers())
    assert response.status_code == 409
    assert not store.list_records('client')


def test_chatgpt_confidential_client_complete_pkce_consent_token_and_tools(client):
    registered = client.post('/api/mcp/oauth-clients', json={'redirect_uri': CALLBACK}, headers=admin_headers()).json()['client']
    verifier = 'connection-verifier-' + 'a' * 48
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response = client.get('/oauth/authorize', params={
        'client_id': registered['client_id'], 'redirect_uri': CALLBACK, 'response_type': 'code',
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'scope': 'read',
        'resource': ORIGIN + '/mcp', 'state': 'connection-test',
    }, follow_redirects=False)
    assert response.status_code == 302
    page = client.get(response.headers['location'])
    transaction = re.search('name="transaction" value="([^"]+)"', page.text).group(1)
    csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
    consent = client.post('/oauth/consent', data={'transaction': transaction, 'csrf': csrf,
                          'password': 'test-password', 'decision': 'allow'}, follow_redirects=False)
    assert consent.status_code == 303
    callback = parse_qs(urlparse(consent.headers['location']).query)
    assert callback['state'] == ['connection-test']
    request = {'grant_type': 'authorization_code', 'client_id': registered['client_id'],
               'client_secret': registered['client_secret'], 'code': callback['code'][0],
               'code_verifier': verifier, 'redirect_uri': CALLBACK, 'resource': ORIGIN + '/mcp'}
    token_response = client.post('/oauth/token', data=request)
    assert token_response.status_code == 200, token_response.text
    token = token_response.json()['access_token']
    tools = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert tools.status_code == 200
    assert 'get_news' in tools.text
    assert client.post('/oauth/token', data=request).status_code == 400


@pytest.mark.parametrize('failure', [None, 'proxy_404', 'old_origin', 'timeout', 'stale_cache', 'invalid_pkce'])
def test_connection_diagnostics_explain_public_discovery_failure(monkeypatch, failure):
    import asyncio
    monkeypatch.setattr(connection, 'settings', lambda: {'enabled': True})
    urls = connection.connection_details(ORIGIN, ORIGIN)

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            fresh = bool(kwargs.get('params'))
            assert (kwargs['headers'].get('Cache-Control') == 'no-cache') is fresh
            if failure == 'timeout':
                raise httpx.ConnectTimeout('private upstream details')
            if url.endswith('/mcp') and '/.well-known/' not in url:
                return httpx.Response(401, headers={'www-authenticate': f'Bearer resource_metadata="{urls["resource_metadata_fallback"]}"'})
            if failure == 'proxy_404' and '/.well-known/' in url and '/oauth/.well-known/' not in url:
                return httpx.Response(404, text='<html>private upstream details</html>')
            if 'resource' in url:
                return httpx.Response(200, json={'resource': urls['resource'], 'authorization_servers': [urls['oauth_issuer']]})
            return httpx.Response(200, json={'issuer': 'http://localhost:7860/oauth' if failure == 'old_origin' or (failure == 'stale_cache' and not fresh) else urls['oauth_issuer'],
                    **{key: urls[key] for key in ('authorization_endpoint', 'token_endpoint', 'registration_endpoint')},
                    'code_challenge_methods_supported': None if failure == 'invalid_pkce' else ['S256']})

    monkeypatch.setattr(connection.httpx, 'AsyncClient', FakeClient)
    result = asyncio.run(connection.check_connection(ORIGIN, ORIGIN))
    assert result['ready'] is (failure is None)
    assert 'private upstream details' not in json.dumps(result)
    if failure == 'proxy_404':
        assert any('转发后端' in row['message'] for row in result['checks'])
    if failure == 'stale_cache':
        assert any('CDN' in row['message'] and row['status'] == 'error' for row in result['checks'])
