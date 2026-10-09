import asyncio
import base64
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import database
from backend.mcp import auth, service, settings as cfg, store
from backend.mcp.server import install_mcp, mcp_lifespan


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DB_NAME', str(tmp_path / 'mcp.db'))
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'test-mcp-master-key')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-admin-password')
    monkeypatch.setenv('CHAT_PASSWORD', '')
    monkeypatch.setattr(cfg, 'exchange_profile', lambda _: {'profile_id': 'account', 'exchange': 'binance', 'market_type': 'swap', 'api_key': 'fake', 'secret': 'fake'})
    cfg.save_profile(cfg.MCPProfile(profile_id='test', name='Test', exchange_profile_id='account', symbols=['ETH/USDT']))
    store.put('settings', 'global', {'enabled': True, 'public_url': 'http://localhost:7860'})
    @asynccontextmanager
    async def lifespan(app):
        async with mcp_lifespan():
            yield
    app = FastAPI(lifespan=lifespan)
    install_mcp(app)
    with TestClient(app, base_url='http://localhost:7860') as client:
        yield client


def rpc(client, token, method, params=None, request_id=1):
    return client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                       json={'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}})


def test_sdk_initialization_tool_list_and_scoped_call(gateway):
    key = auth.create_api_key('Reader', ['read'], ['test'])['secret']
    init = rpc(gateway, key, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
    assert init.status_code == 200
    assert init.json()['result']['serverInfo']['name'] == 'Crypto Agent'
    listing = rpc(gateway, key, 'tools/list').json()['result']['tools']
    assert {'execute_trade', 'get_market', 'get_news'} <= {tool['name'] for tool in listing}
    response = rpc(gateway, key, 'tools/call', {'name': 'list_profiles', 'arguments': {}})
    assert 'Test' in response.text
    assert 'fake' not in response.text
    denied = rpc(gateway, key, 'tools/call', {'name': 'execute_trade', 'arguments': {
        'profile_id': 'test', 'symbol': 'ETH/USDT', 'tool_name': 'cancel_orders_real',
        'arguments': {'order_id': '1', 'reason': 'test'}, 'operation_id': 'operation-123'}})
    assert 'Missing MCP scope: cancel' in denied.text
    assert gateway.get('/api/mcp', headers={'Authorization': 'Bearer ' + key}).status_code == 401


def test_unauthenticated_discovery_and_revoked_key(gateway):
    response = gateway.post('/mcp', json={})
    assert response.status_code == 401
    assert '/.well-known/oauth-protected-resource/mcp' in response.headers['www-authenticate']
    metadata = gateway.get('/.well-known/oauth-authorization-server/oauth').json()
    assert metadata['code_challenge_methods_supported'] == ['S256']
    assert metadata['token_endpoint'] == 'http://localhost:7860/oauth/token'
    key = auth.create_api_key('Read', ['read'], [])
    record = store.get('key', key['id'])
    record['revoked'] = True
    store.put('key', key['id'], record)
    assert rpc(gateway, key['secret'], 'tools/list').status_code == 401


def oauth_code(client):
    reg = client.post('/oauth/register', json={'client_name': 'Test Client', 'redirect_uris': ['http://localhost:12345/callback'], 'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'], 'scope': 'read trade cancel'})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()['client_id']
    verifier = 'a' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = dict(client_id=client_id, response_type='code', redirect_uri='http://localhost:12345/callback',
                  code_challenge=challenge, code_challenge_method='S256', scope='read trade cancel', state='test-state', resource='http://localhost:7860/mcp')
    authorization = client.get('/oauth/authorize', params=params, follow_redirects=False)
    assert authorization.status_code in {302, 303, 307}, authorization.text
    page = client.get(authorization.headers['location'])
    assert page.status_code == 200, (authorization.headers['location'], page.text)
    transaction = re.search('name="transaction" value="([^"]+)"', page.text).group(1)
    csrf = re.search('name="csrf" value="([^"]+)"', page.text).group(1)
    denied = client.post('/oauth/consent', data={'transaction': transaction, 'csrf': 'wrong', 'password': 'test-admin-password', 'decision': 'allow'})
    assert denied.status_code == 403
    consent = client.post('/oauth/consent', data={'transaction': transaction, 'csrf': csrf, 'password': 'test-admin-password', 'profile_ids': 'test', 'decision': 'allow'}, follow_redirects=False)
    assert consent.status_code == 303, consent.text
    callback = parse_qs(urlparse(consent.headers['location']).query)
    assert callback['state'] == ['test-state']
    return dict(grant_type='authorization_code', client_id=client_id, code=callback['code'][0], code_verifier=verifier, redirect_uri=params['redirect_uri'])


def test_oauth_pkce_refresh_rotation_revocation(gateway):
    request = oauth_code(gateway)
    wrong = gateway.post('/oauth/token', data={**request, 'code_verifier': 'b' * 64})
    assert wrong.status_code == 400 and wrong.json()['error'] == 'invalid_grant'
    token = gateway.post('/oauth/token', data=request)
    assert token.status_code == 200, token.text
    token = token.json()
    assert rpc(gateway, token['access_token'], 'tools/list').status_code == 200
    assert gateway.post('/oauth/token', data=request).status_code == 400
    refresh_args = {'client_id': request['client_id'], 'grant_type': 'refresh_token', 'refresh_token': token['refresh_token']}
    new = gateway.post('/oauth/token', data=refresh_args)
    assert new.status_code == 200, new.text
    assert gateway.post('/oauth/token', data=refresh_args).status_code == 400
    assert rpc(gateway, token['access_token'], 'tools/list').status_code == 401
    new = new.json()
    revoke = gateway.post('/oauth/revoke', data={'client_id': request['client_id'], 'token': new['refresh_token'], 'token_type_hint': 'refresh_token'})
    assert revoke.status_code == 200, revoke.text
    assert rpc(gateway, new['access_token'], 'tools/list').status_code == 401


def test_durable_idempotence_and_scope_identity(gateway, monkeypatch):
    import backend.agent.tool_registry as registry
    monkeypatch.setattr(service, 'trading_tools', lambda _: [{'name': 'cancel_orders_real'}])
    calls = []
    monkeypatch.setattr(registry, 'run_trade_tool', lambda *args, **kwargs: calls.append((args, kwargs)) or '{"status":"cancelled"}')
    caller = {'id': 'key:a', 'scopes': ['read', 'cancel'], 'profile_ids': ['test']}
    request = ('test', 'ETH/USDT', 'cancel_orders_real', {'order_id': '1', 'reason': 'test'}, 'stable-operation')
    first = service.execute(caller, *request)
    second = service.execute(caller, *request)
    assert first == second and len(calls) == 1
    mismatch = service.execute(caller, 'test', 'ETH/USDT', 'cancel_orders_real', {'order_id': '2', 'reason': 'test'}, 'stable-operation')
    assert mismatch['status'] == 'failed' and len(calls) == 1
    crossed = service.execute({**caller, 'id': 'key:b'}, *request)
    assert crossed['status'] == 'failed' and len(calls) == 1
    assert store.audit_rows()


def test_actual_leverage_guard_before_any_protection_side_effect(gateway, monkeypatch):
    from backend.mcp.guard import assert_entry_allowed
    from backend.utils.position_protection import PositionProtection
    from backend.utils.independent_exits import IndependentExits
    exchange = SimpleNamespace(id='binanceusdm', apiKey='fake', market=lambda symbol: {'symbol': symbol},
                               fetch_leverage=lambda symbol: {'longLeverage': 20, 'shortLeverage': 20},
                               fetch_positions=lambda symbols: [])
    mt = SimpleNamespace(config_id='mcp:test', exchange=exchange)
    for cls in (PositionProtection, IndependentExits):
        protection = cls(mt)
        monkeypatch.setattr(protection, '_market', lambda symbol: pytest.fail('Protection must not start before leverage approval'))
        with pytest.raises(ValueError, match='Actual exchange leverage'):
            protection.open('ETH/USDT', SimpleNamespace())
        with pytest.raises(ValueError, match='Actual exchange leverage'):
            protection.amend_entry('ETH/USDT', 'order', price=100, reason='test')
    assert_entry_allowed(SimpleNamespace(config_id='internal', exchange=exchange), 'ETH/USDT')
    exchange.fetch_leverage = lambda symbol: {'longLeverage': 5, 'shortLeverage': 5}
    assert_entry_allowed(mt, 'ETH/USDT')
    exchange.fetch_leverage = lambda symbol: {}
    with pytest.raises(ValueError, match='Cannot verify'):
        assert_entry_allowed(mt, 'ETH/USDT')


def test_batch_preflight_checks_entries_before_protection_dispatch(gateway, monkeypatch):
    from backend.agent.trade_batch import execute_trade_actions
    from backend.config import config
    from backend.utils.market_data import MarketTool
    import backend.mcp.guard as guard
    monkeypatch.setattr(config, 'get_config_by_id', lambda _: {'mode': 'REAL', 'exit_mode': 'attached_required'})
    calls = []
    monkeypatch.setattr(MarketTool, '__init__', lambda self, **kwargs: None)
    monkeypatch.setattr(guard, 'assert_entry_allowed', lambda *args: (_ for _ in ()).throw(ValueError('actual leverage too high')))
    import backend.agent.trade_batch as batch
    monkeypatch.setattr(batch, '_dispatch', lambda *args: calls.append(args))
    actions = [dict(action='update_protection', pos_side='LONG', stop_loss=90, reason='test'),
               dict(action='open', order=dict(action='BUY_LIMIT', entry_price=100, amount=1, stop_loss=90, take_profit=110, reason='test'))]
    with pytest.raises(ValueError, match='actual leverage'):
        execute_trade_actions.func(actions=actions, config_id='mcp:test', symbol='ETH/USDT')
    assert not calls


def test_foreign_and_untracked_positions_fail_closed_but_owned_reduction_allowed(gateway, monkeypatch):
    from backend.mcp.guard import assert_position_owner, preflight_tool
    from backend.utils.execution_ledger import account_scope
    from backend.utils.market_data import MarketTool
    exchange = SimpleNamespace(id='binanceusdm', apiKey='fake',
        market=lambda symbol: {'symbol': 'ETH/USDT:USDT', 'contract': True, 'linear': True},
        fetch_positions=lambda symbols: [{'side': 'long', 'contracts': 2, 'leverage': 20}],
        fetch_leverage=lambda symbol: pytest.fail('Reductions must not enforce entry leverage cap'),
        fetch_my_trades=lambda *args, **kwargs: [{'id': 'fill-1', 'order': 'entry-1', 'amount': 3}, {'id': 'fill-2', 'order': 'exit-1', 'amount': 1}])
    mt = SimpleNamespace(config_id='mcp:test', market_type='swap', exchange=exchange)
    with pytest.raises(ValueError, match='Untracked exchange position'):
        assert_position_owner(mt, 'ETH/USDT')
    scope = account_scope(exchange, mt.config_id)
    plan = {'account_scope': scope, 'symbol': 'ETH/USDT:USDT', 'side': 'LONG', 'state': 'ACTIVE',
            'entries': [{'id': 'entry-1', 'created_at': 100}], 'exits': [{'id': 'exit-1'}]}
    with store.connection() as conn:
        conn.execute('CREATE TABLE real_protection_plans(config_id TEXT,symbol TEXT,side TEXT,payload TEXT)')
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('mcp:test', plan['symbol'], 'LONG', json.dumps(plan)))
        conn.commit()
    assert_position_owner(mt, 'ETH/USDT')
    monkeypatch.setattr(MarketTool, '__new__', lambda cls, **kwargs: mt)
    preflight_tool('close_position_real', {}, mt.config_id, 'ETH/USDT')
    exchange.fetch_my_trades = lambda *args, **kwargs: [{'id': 'fill-1', 'order': 'entry-1', 'amount': 1}, {'id': 'foreign', 'order': 'manual-1', 'amount': 1}]
    with pytest.raises(ValueError, match='foreign fills'):
        assert_position_owner(mt, 'ETH/USDT')
    # Cancellation only still delegates to the existing per-order ownership gate.
    preflight_tool('cancel_orders_real', {}, mt.config_id, 'ETH/USDT')
    with store.connection() as conn:
        conn.execute('UPDATE real_protection_plans SET config_id=?', ('internal-agent',))
        conn.commit()
    with pytest.raises(ValueError, match='Another strategy'):
        assert_position_owner(mt, 'ETH/USDT')


def test_unknown_operation_never_replays_and_caller_cannot_read_foreign_receipt(gateway, monkeypatch):
    import backend.agent.tool_registry as registry
    monkeypatch.setattr(service, 'trading_tools', lambda _: [{'name': 'cancel_orders_real'}])
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('Exchange acknowledgement unavailable')
    monkeypatch.setattr(registry, 'run_trade_tool', timeout)
    caller = {'id': 'key:a', 'scopes': ['read', 'cancel'], 'profile_ids': ['test']}
    args = ('test', 'ETH/USDT', 'cancel_orders_real', {'order_id': '1', 'reason': 'test'}, 'unknown-operation')
    first = service.execute(caller, *args)
    assert first['status'] == 'unknown'
    assert service.execute(caller, *args) == first and len(calls) == 1
    assert service.operation(caller, 'test', 'unknown-operation')['state'] == 'unknown'
    with pytest.raises(ValueError, match='not found'):
        service.operation({**caller, 'id': 'key:b'}, 'test', 'unknown-operation')


def test_admin_and_mcp_authentication_are_separate_and_admin_responses_not_cached(gateway):
    import jwt
    from backend.app.core import security
    token = security.create_access_token()
    assert rpc(gateway, token, 'tools/list').status_code == 401
    headers = {'Authorization': 'Bearer ' + token}
    assert gateway.get('/api/mcp', headers=headers).headers['cache-control'] == 'no-store'
    response = gateway.post('/api/mcp/keys', headers=headers, json={'name': 'Visible only to admin'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    raw = response.json()['key']['secret']
    assert gateway.get('/api/mcp', headers={'Authorization': 'Bearer ' + raw}).status_code == 401
    crossed = jwt.encode({'sub': 'admin', 'aud': 'http://localhost:7860/mcp', 'exp': int(time.time()) + 60}, security.JWT_SECRET, algorithm='HS256')
    assert gateway.get('/api/mcp', headers={'Authorization': 'Bearer ' + crossed}).status_code == 401
    auth_row = {'client_id': 'test', 'scopes': ['read'], 'expires_at': time.time() - 1,
                'resource': 'http://localhost:7860/mcp', 'claims': {'grant_id': 'expired'}}
    store.put('access', store.digest('expired-access'), auth_row)
    assert rpc(gateway, 'expired-access', 'tools/list').status_code == 401


def test_mcp_settings_migration_reencrypts_keys_and_rolls_back_invalid_profile(gateway, monkeypatch):
    from backend.mcp.transfer import export_settings, import_settings
    key = auth.create_api_key('Portable', ['read', 'cancel'], ['test'])
    original_ciphertext = store.get('key', key['id'])['secret']
    exported = export_settings(True)
    redacted = json.dumps(export_settings(False))
    assert key['secret'] not in redacted and key['digest'] not in redacted
    with store.connection() as conn:
        conn.execute('CREATE TABLE exchange_profiles(profile_id TEXT,market_type TEXT)')
        conn.execute("INSERT INTO exchange_profiles VALUES('account','swap')")
        conn.commit()
    monkeypatch.setenv('CONFIG_MASTER_KEY', 'different-destination-master-key')
    import_settings(exported)
    assert store.get('key', key['id'])['secret'] != original_ciphertext
    assert auth.admin_keys()[0]['secret'] == key['secret']
    bad = {**exported, 'settings': {'enabled': False}, 'profiles': [{**exported['profiles'][0], 'exchange_profile_id': 'missing'}]}
    with pytest.raises(ValueError, match='missing or incompatible'):
        import_settings(bad)
    assert cfg.settings()['enabled'] is True
    assert cfg.profiles()[0]['exchange_profile_id'] == 'account'


def test_account_and_profile_edits_preserve_active_mcp_lifecycle(gateway, monkeypatch):
    import backend.config_store as config_store
    from backend.mcp.config_guard import account_change_hook, assert_profile_transition
    previous_account = {'profile_id': 'account', 'exchange': 'binance', 'market_type': 'swap', 'api_key': 'same-key', 'secret': 'same-secret'}
    next_accounts = [dict(previous_account)]
    monkeypatch.setattr(config_store, 'load_effective_runtime_snapshot', lambda: {'exchange_profiles': [previous_account]})
    monkeypatch.setattr(config_store, 'load_runtime_snapshot', lambda **kwargs: {'exchange_profiles': next_accounts})
    validate = account_change_hook()
    profile = cfg.profiles()[0]
    with store.connection() as conn:
        conn.execute('CREATE TABLE real_protection_plans(config_id TEXT,symbol TEXT,side TEXT,payload TEXT)')
        conn.execute('INSERT INTO real_protection_plans VALUES(?,?,?,?)', ('mcp:test', 'ETH/USDT:USDT', 'LONG', '{"state":"ACTIVE"}'))
        validate(conn)  # Saving the same plaintext credentials is not rotation.
        next_accounts[0]['api_key'] = 'rotated-key'
        with pytest.raises(ValueError, match='credentials cannot change'):
            validate(conn)
        with pytest.raises(ValueError, match='still has positions'):
            assert_profile_transition(conn, profile, {**profile, 'symbols': ['BTC/USDT']})
        with pytest.raises(ValueError, match='still has positions'):
            assert_profile_transition(conn, profile, {**profile, 'exit_mode': 'independent_exits'})
        conn.execute('UPDATE real_protection_plans SET payload=?', ('{"state":"DONE"}',))
        validate(conn)  # Credential rotation is allowed after the lifecycle ends.
        next_accounts.clear()
        with pytest.raises(ValueError, match='referenced by MCP'):
            validate(conn)


def test_exchange_secrets_and_nonfinite_values_never_escape_tools_or_audit(gateway, monkeypatch):
    import backend.agent.tool_registry as registry
    monkeypatch.setattr(service, 'trading_tools', lambda _: [{'name': 'cancel_orders_real'}])
    caller = {'id': 'key:clean', 'scopes': ['read', 'cancel'], 'profile_ids': ['test']}
    args = ('test', 'ETH/USDT', 'cancel_orders_real', {'order_id': '1', 'reason': 'test'}, 'redact-operation')
    monkeypatch.setattr(registry, 'run_trade_tool', lambda *args, **kwargs: json.dumps({'status': 'cancelled', 'apiKey': 'fake', 'message': 'exchange echoed fake', 'cost': float('nan')}))
    result = service.execute(caller, *args)
    encoded = json.dumps(result)
    assert 'fake' not in encoded and 'NaN' not in encoded and result['result']['cost'] is None
    def error(*args, **kwargs):
        raise TimeoutError('signed exchange request fake signature=private-auth-header')
    monkeypatch.setattr(registry, 'run_trade_tool', error)
    unknown = service.execute(caller, *args[:-1], 'redact-unknown-operation')
    assert unknown['status'] == 'unknown' and 'private-auth-header' not in json.dumps(unknown)
    monkeypatch.setattr(service, 'market_tool', error)
    with pytest.raises(ValueError, match='TimeoutError') as failure:
        service.query(caller, 'balance', profile_id='test')
    assert 'fake' not in str(failure.value)
    assert 'fake' not in json.dumps(store.audit_rows())
    assert 'private-auth-header' not in json.dumps(store.audit_rows())


def test_oauth_consent_can_grant_read_only_and_admin_can_revoke(gateway):
    from backend.app.core.security import create_access_token
    request = oauth_code(gateway)
    token = gateway.post('/oauth/token', data=request).json()
    assert token['scope'] == 'read'
    headers = {'Authorization': 'Bearer ' + create_access_token()}
    overview = gateway.get('/api/mcp', headers=headers).json()
    connection = overview['oauth_connections'][0]
    assert connection['scopes'] == ['read'] and connection['profile_ids'] == ['test']
    assert token['refresh_token'] not in json.dumps(overview)
    assert gateway.delete('/api/mcp/oauth-grants/' + connection['id'], headers=headers).status_code == 200
    assert rpc(gateway, token['access_token'], 'tools/list').status_code == 401
