import base64
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.app.core import security
from backend.mcp import auth, consent, store
from test_mcp_connection import CALLBACK, ORIGIN, client


def start(client, *, state='consent-test'):
    registration = client.post('/oauth/register', json={
        'client_name': 'Browser test', 'redirect_uris': [CALLBACK],
        'token_endpoint_auth_method': 'none', 'scope': 'read',
    }).json()
    verifier = 'v' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response = client.get('/oauth/authorize', params={
        'client_id': registration['client_id'], 'response_type': 'code', 'redirect_uri': CALLBACK,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'scope': 'read',
        'resource': ORIGIN + '/mcp', 'state': state,
    }, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers['location']
    page = client.get(location)
    fields = {key: re.search(f'name="{key}" value="([^"]+)"', page.text).group(1)
              for key in ('transaction', 'csrf')}
    return location, fields, registration['client_id'], verifier, page


def approve(client, fields, **changes):
    return client.post('/oauth/consent', data={**fields, 'password': 'test-password', 'decision': 'allow', **changes},
                       follow_redirects=False)


def token_request(response, client_id, verifier):
    return {'grant_type': 'authorization_code', 'client_id': client_id,
            'code': parse_qs(urlparse(response.headers['location']).query)['code'][0],
            'redirect_uri': CALLBACK, 'code_verifier': verifier, 'resource': ORIGIN + '/mcp'}


def test_consent_csp_allows_only_self_and_validated_callback_origin(client):
    _, _, _, _, page = start(client)
    csp = page.headers['content-security-policy']
    assert "form-action 'self' https://chatgpt.com;" in csp
    assert '*' not in csp and "frame-ancestors 'none'" in csp
    assert page.headers['referrer-policy'] == 'no-referrer'
    assert page.headers['cache-control'] == 'no-store'


def test_refresh_and_two_oauth_tabs_keep_independent_stable_csrf(client):
    location, first, _, _, _ = start(client, state='first')
    expiry = store.get('pending', store.digest(first['transaction']))['expires_at']
    _, second, _, _, _ = start(client, state='second')
    refreshed = client.get(location)
    assert f'name="csrf" value="{first["csrf"]}"' in refreshed.text
    assert store.get('pending', store.digest(first['transaction']))['expires_at'] == expiry
    assert approve(client, first).status_code == 303
    assert approve(client, second).status_code == 303
    assert len(store.list_records('code')) == 2


def test_post_retry_and_browser_refresh_reuse_one_unconsumed_code(client):
    location, fields, client_id, verifier, _ = start(client)
    first = approve(client, fields)
    assert first.status_code == 303
    assert approve(client, fields).headers['location'] == first.headers['location']
    assert client.get(location, follow_redirects=False).headers['location'] == first.headers['location']
    assert len(store.list_records('code')) == 1
    pending = store.get('pending', store.digest(fields['transaction']))
    assert fields['csrf'] not in json.dumps(pending)
    assert token_request(first, client_id, verifier)['code'] not in json.dumps(pending)
    assert client.post('/oauth/token', data=token_request(first, client_id, verifier)).status_code == 200
    finished = client.get(location, follow_redirects=False)
    assert finished.status_code == 200 and '授权已经处理' in finished.text
    assert 'location' not in finished.headers and not store.list_records('code')
    assert approve(client, fields).status_code == 200


def test_completed_url_does_not_disclose_code_to_another_browser(client):
    location, fields, _, _, _ = start(client)
    first = approve(client, fields)
    callback = first.headers['location']
    client.cookies.clear()
    stranger = client.get(location, follow_redirects=False)
    assert stranger.status_code == 200 and 'location' not in stranger.headers
    assert callback not in stranger.text and not stranger.cookies


def test_cookie_failure_and_password_failure_offer_recovery_without_consuming(client):
    location, fields, _, _, _ = start(client)
    incorrect = approve(client, fields, password='bad-password')
    assert incorrect.status_code == 401 and '管理员密码不正确' in incorrect.text
    assert 'bad-password' not in incorrect.text and '<form' in incorrect.text
    csrf_failure = approve(client, {**fields, 'csrf': 'wrong'})
    assert csrf_failure.status_code == 403 and '重新打开授权页面' in csrf_failure.text
    client.cookies.clear()
    missing_cookie = approve(client, fields)
    assert missing_cookie.status_code == 403 and 'Cookie' in missing_cookie.text
    assert not store.list_records('code')
    client.get(location)
    assert approve(client, fields).status_code == 303


def test_expired_request_can_restart_through_full_sdk_validation(client):
    location, fields, _, _, _ = start(client)
    key = store.digest(fields['transaction'])
    pending = store.get('pending', key)
    pending['expires_at'] = time.time() - 1
    store.put('pending', key, pending)
    expired = client.get(location)
    assert expired.status_code == 400 and '授权请求已过期' in expired.text
    restart = html.unescape(re.search(r'href="([^"]+/oauth/authorize\?[^"]+)"', expired.text).group(1))
    query = parse_qs(urlparse(restart).query)
    assert query['redirect_uri'] == [CALLBACK] and query['code_challenge_method'] == ['S256']
    assert query['code_challenge'] == [pending['params']['code_challenge']]
    restarted = client.get(restart, follow_redirects=False)
    assert restarted.status_code == 302 and restarted.headers['location'] != location
    assert not store.list_records('code')
    unknown = client.get('/oauth/consent?transaction=missing&redirect_uri=https://evil.test')
    assert unknown.status_code == 400 and 'evil.test' not in unknown.text


def test_expired_authorization_code_does_not_mint_another_on_refresh(client):
    location, fields, _, _, _ = start(client)
    approve(client, fields)
    pending = store.get('pending', store.digest(fields['transaction']))
    code = store.get('code', pending['code_digest'])
    code['expires_at'] = time.time() - 1
    store.put('code', pending['code_digest'], code)
    response = client.get(location, follow_redirects=False)
    assert response.status_code == 400 and '重新打开授权页面' in response.text
    assert len(store.list_records('code')) == 1


def test_two_provider_instances_share_one_cookie_bound_completion(client):
    location, fields, client_id, verifier, _ = start(client)
    provider = auth.AuthorizationProvider(ORIGIN)
    app = Starlette(routes=[Route('/oauth/consent', provider.consent, methods=['GET', 'POST'])])
    with TestClient(app, base_url=ORIGIN, cookies=client.cookies) as worker:
        assert fields['csrf'] in worker.get(location).text
        response = approve(worker, fields)
    assert client.get(location, follow_redirects=False).headers['location'] == response.headers['location']
    assert client.post('/oauth/token', data=token_request(response, client_id, verifier)).status_code == 200


def test_simultaneous_http_approvals_commit_one_code(client, monkeypatch):
    _, fields, _, _, _ = start(client)
    barrier = Barrier(2)
    monkeypatch.setattr(security, 'authenticate_password', lambda _: barrier.wait(timeout=10) is not None)

    def submit(_):
        provider = auth.AuthorizationProvider(ORIGIN)
        app = Starlette(routes=[Route('/oauth/consent', provider.consent, methods=['GET', 'POST'])])
        with TestClient(app, base_url=ORIGIN, cookies=client.cookies) as worker:
            return approve(worker, fields)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert all(response.status_code == 303 for response in responses)
    assert responses[0].headers['location'] == responses[1].headers['location']
    assert len(store.list_records('code')) == 1
    assert len([row for row in store.audit_rows() if row['tool'] == 'oauth_authorize']) == 1


def test_completion_failure_rolls_back_code_and_pending_together(client, monkeypatch):
    _, fields, _, _, _ = start(client)
    encrypt = auth.encrypt

    def fail_callback(value):
        if value.startswith(CALLBACK):
            raise RuntimeError('simulated storage encryption failure')
        return encrypt(value)

    with monkeypatch.context() as patch:
        patch.setattr(auth, 'encrypt', fail_callback)
        with pytest.raises(RuntimeError, match='simulated'):
            approve(client, fields)
    assert not store.list_records('code')
    assert not store.get('pending', store.digest(fields['transaction'])).get('completed_at')
    assert approve(client, fields).status_code == 303


def test_cancel_replays_only_original_denial_and_keeps_state(client):
    location, fields, _, _, _ = start(client, state='denial-state')
    response = approve(client, fields, password='', decision='deny')
    query = parse_qs(urlparse(response.headers['location']).query)
    assert query['error'] == ['access_denied'] and query['state'] == ['denial-state']
    assert client.get(location, follow_redirects=False).headers['location'] == response.headers['location']
    assert not store.list_records('code')


def test_legacy_pending_form_can_be_refreshed_after_deployment(client):
    location, fields, _, _, _ = start(client)
    key = store.digest(fields['transaction'])
    pending = store.get('pending', key)
    pending.pop('csrf_token')
    store.put('pending', key, pending)
    page = client.get(location)
    updated = {key: re.search(f'name="{key}" value="([^"]+)"', page.text).group(1)
               for key in ('transaction', 'csrf')}
    assert updated['csrf'] != fields['csrf']
    assert approve(client, updated).status_code == 303
