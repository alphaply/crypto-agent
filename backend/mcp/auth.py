"""SDK OAuth 2.1 provider; SDK handlers validate PKCE, redirects and client auth."""
from __future__ import annotations

import html
import secrets
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, RegistrationError, TokenError, construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.datastructures import FormData

from . import store
from .settings import SCOPES, profiles, settings


def encrypt(value):
    from backend.config_store import _encrypt_secret
    return _encrypt_secret(value)


def decrypt(value):
    from backend.config_store import _decrypt_secret
    return _decrypt_secret(value)


def create_api_key(name, scopes, profile_ids):
    validate_grant(scopes, profile_ids)
    raw = 'ca_mcp_' + secrets.token_urlsafe(32)
    key_id = secrets.token_hex(12)
    record = dict(id=key_id, name=name, scopes=scopes, profile_ids=profile_ids,
                  created_at=time.time(), revoked=False, secret=encrypt(raw), digest=store.digest(raw))
    store.put('key', key_id, record)
    store.put('key_lookup', record['digest'], {'id': key_id})
    return {**record, 'secret': raw}


def admin_keys():
    return [{**row, 'secret': decrypt(row['secret']) if not row['revoked'] else ''} for row in store.list_records('key')]


def admin_connections():
    clients = {row['client_id']: row.get('client_name') or row['client_id'] for row in store.list_records('client')}
    grants = {}
    for row in store.list_records('refresh'):
        grants[row['grant_id']] = {'id': row['grant_id'], 'name': clients.get(row['client_id'], row['client_id']),
            'client_id': row['client_id'], 'scopes': row['scopes'], 'profile_ids': row['profile_ids'],
            'expires_at': row['expires_at'], 'revoked': bool(store.get('revoked_grant', row['grant_id']))}
    return list(grants.values())


def validate_grant(scopes, profile_ids):
    if not scopes or not set(scopes) <= set(SCOPES) or 'read' not in scopes:
        raise ValueError('Scopes must include read and may include trade/cancel')
    known = {row['profile_id'] for row in profiles()}
    if not set(profile_ids) <= known:
        raise ValueError('Unknown MCP profile in grant')
    if set(scopes) & {'trade', 'cancel'} and not profile_ids:
        raise ValueError('Trading permissions require at least one explicit profile')


async def sdk_revoke(provider, request):
    """Normalize the SDK 1.30 required-nullable secret field for public clients.

    SDK ClientAuthenticator still enforces each registered authentication method.
    No grant validation or revocation protocol is reimplemented here.
    """
    from mcp.server.auth.handlers.revoke import RevocationHandler
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator
    data = dict(await request.form())
    data.setdefault('client_secret', None)

    class NormalizedRequest:
        def __getattr__(self, name):
            return getattr(request, name)

        async def form(self):
            return FormData(data)

    return await RevocationHandler(provider, ClientAuthenticator(provider)).handle(NormalizedRequest())


class MCPCode(AuthorizationCode):
    profile_ids: list[str]
    grant_id: str


class MCPRefresh(RefreshToken):
    profile_ids: list[str]
    grant_id: str
    access_digest: str


class AuthorizationProvider:
    def __init__(self, public_url):
        self.public_url = public_url.rstrip('/')
        self.resource = self.public_url + '/mcp'

    async def get_client(self, client_id):
        row = store.get('client', client_id)
        if row and row.get('client_secret'):
            row['client_secret'] = decrypt(row['client_secret'])
        return OAuthClientInformationFull.model_validate(row) if row else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris:
            parsed = urlparse(str(uri))
            if parsed.fragment or parsed.username or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'})):
                raise RegistrationError('invalid_redirect_uri', 'Use HTTPS or loopback HTTP redirect URIs')
        if len(store.list_records('client')) >= 1000:
            raise RegistrationError('invalid_client_metadata', 'Client registration limit reached')
        row = client_info.model_dump(mode='json')
        if row.get('client_secret'):
            row['client_secret'] = encrypt(row['client_secret'])
        store.put('client', client_info.client_id, row)

    async def authorize(self, client, params):
        if not settings()['enabled']:
            raise AuthorizeError('access_denied', 'MCP is disabled')
        if params.resource and params.resource != self.resource:
            raise AuthorizeError('invalid_request', 'The resource must be this MCP endpoint')
        requested = params.scopes or ['read']
        if not set(requested) <= set(SCOPES):
            raise AuthorizeError('invalid_scope', 'Unsupported scope')
        transaction = secrets.token_urlsafe(32)
        store.put('pending', store.digest(transaction), {
            'client_id': client.client_id, 'client_name': client.client_name or client.client_id,
            'params': params.model_dump(mode='json'), 'expires_at': time.time() + 600,
        })
        return self.public_url + '/oauth/consent?transaction=' + transaction

    async def load_authorization_code(self, client, authorization_code):
        row = store.get('code', store.digest(authorization_code))
        return MCPCode.model_validate({**row, 'code': authorization_code}) if row and row['client_id'] == client.client_id else None

    def _issue(self, client_id, scopes, profile_ids, grant_id):
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        access_row = dict(client_id=client_id, scopes=scopes, expires_at=now + 3600,
                          resource=self.resource, subject='admin',
                          claims={'profile_ids': profile_ids, 'grant_id': grant_id, 'principal': 'oauth:' + grant_id})
        refresh_row = dict(client_id=client_id, scopes=scopes, expires_at=now + 30 * 86400,
                           resource=self.resource, subject='admin', profile_ids=profile_ids,
                           grant_id=grant_id, access_digest=store.digest(access))
        store.put('access', store.digest(access), access_row)
        store.put('refresh', store.digest(refresh), refresh_row)
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=3600, scope=' '.join(scopes), token_type='Bearer')

    async def exchange_authorization_code(self, client, authorization_code):
        row = store.pop('code', store.digest(authorization_code.code))
        if not row or row['client_id'] != client.client_id or row['expires_at'] < time.time():
            raise TokenError('invalid_grant', 'Authorization code consumed or expired')
        return self._issue(client.client_id, row['scopes'], row['profile_ids'], row['grant_id'])

    async def load_refresh_token(self, client, refresh_token):
        row = store.get('refresh', store.digest(refresh_token))
        if (not row or row['client_id'] != client.client_id or row.get('resource') != self.resource
                or store.get('revoked_grant', row['grant_id'])):
            return None
        return MCPRefresh.model_validate({**row, 'token': refresh_token})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        row = store.pop('refresh', store.digest(refresh_token.token))
        if (not row or row['expires_at'] < time.time() or row['client_id'] != client.client_id or row.get('resource') != self.resource
                or not set(scopes) <= set(row['scopes']) or store.get('revoked_grant', row['grant_id'])):
            raise TokenError('invalid_grant', 'Refresh token consumed, revoked or expired')
        store.pop('access', row['access_digest'])
        return self._issue(client.client_id, scopes, row['profile_ids'], row['grant_id'])

    async def load_access_token(self, token):
        if not settings()['enabled']:
            return None
        lookup = store.get('key_lookup', store.digest(token))
        if lookup:
            row = store.get('key', lookup['id'])
            if not row or row['revoked'] or row['digest'] != store.digest(token):
                return None
            return AccessToken(token=token, client_id='key:' + row['id'], scopes=row['scopes'], resource=self.resource,
                               subject='admin', claims={'profile_ids': row['profile_ids'], 'principal': 'key:' + row['id']})
        row = store.get('access', store.digest(token))
        if (not row or row['expires_at'] <= time.time() or row['resource'] != self.resource
                or store.get('revoked_grant', row['claims']['grant_id'])):
            return None
        return AccessToken.model_validate({**row, 'token': token})

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        grant = getattr(token, 'grant_id', None) or (getattr(token, 'claims', None) or {}).get('grant_id')
        if grant:
            store.put('revoked_grant', grant, {'revoked_at': time.time()})

    async def consent(self, request: Request):
        from backend.app.core.security import authenticate_password, check_login_allowed, record_login_failure, reset_login_attempts
        transaction = request.query_params.get('transaction', '') if request.method == 'GET' else str((await request.form()).get('transaction', ''))
        pending = store.get('pending', store.digest(transaction))
        if not pending or pending['expires_at'] < time.time():
            return HTMLResponse('Authorization request expired. Restart the connection.', status_code=400)
        esc = html.escape
        if request.method == 'GET':
            csrf = secrets.token_urlsafe(32)
            pending['csrf'] = store.digest(csrf)
            store.put('pending', store.digest(transaction), pending)
            requested = list(dict.fromkeys(['read'] + (pending['params'].get('scopes') or [])))
            scope_options = ''.join(f'<label><input type="checkbox" name="scopes" value="{esc(scope, quote=True)}" checked>{esc(scope)}</label>' for scope in requested if scope != 'read')
            options = ''.join(f'<label><input type="checkbox" name="profile_ids" value="{esc(p["profile_id"], quote=True)}">{esc(p["name"])}</label>' for p in profiles() if p.get('enabled'))
            response = HTMLResponse(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MCP 授权</title>
            <style>body{{font:16px system-ui;background:#f5f7fb;color:#14233b;max-width:520px;margin:8vh auto;padding:24px}}form{{display:grid;gap:18px;background:white;padding:28px;border-radius:18px}}label{{display:block}}input[type=password]{{padding:12px}}button{{padding:12px}}</style>
            <h1>连接 Crypto Agent</h1><form method="post"><p>客户端：{esc(pending['client_name'])}</p><p>查询权限 read；选择额外授权：</p>{scope_options}
            <p>选择允许访问的 MCP 交易配置：</p>{options}<input type="hidden" name="transaction" value="{esc(transaction, quote=True)}"><input type="hidden" name="csrf" value="{esc(csrf, quote=True)}">
            <label>管理员密码 <input name="password" type="password" autocomplete="current-password" required></label>
            <button name="decision" value="allow">授权连接</button><button name="decision" value="deny" formnovalidate>取消</button></form></html>''')
            response.set_cookie('mcp_consent', csrf, httponly=True, secure=self.public_url.startswith('https:'), samesite='lax', max_age=600, path='/oauth/consent')
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            return response
        form = await request.form()
        csrf = str(form.get('csrf', ''))
        if (not csrf or not secrets.compare_digest(csrf, request.cookies.get('mcp_consent', ''))
                or not secrets.compare_digest(store.digest(csrf), pending.get('csrf', ''))):
            return JSONResponse({'error': 'Invalid consent session'}, status_code=403)
        params = pending['params']
        if form.get('decision') != 'allow':
            store.pop('pending', store.digest(transaction))
            return RedirectResponse(construct_redirect_uri(params['redirect_uri'], error='access_denied', state=params.get('state')), status_code=303)
        ip = request.client.host if request.client else 'unknown'
        allowed, reason = check_login_allowed(ip)
        if not allowed:
            return JSONResponse({'error': reason}, status_code=429)
        if not authenticate_password(str(form.get('password', ''))):
            record_login_failure(ip)
            return JSONResponse({'error': 'Invalid administrator password'}, status_code=401)
        reset_login_attempts(ip)
        selected_scopes = [str(value) for value in form.getlist('scopes')]
        if not set(selected_scopes) <= set(params.get('scopes') or []):
            return JSONResponse({'error': 'Cannot grant scopes not requested by this client'}, status_code=400)
        scopes = list(dict.fromkeys(['read'] + selected_scopes))
        profile_ids = list(dict.fromkeys(str(value) for value in form.getlist('profile_ids')))
        try:
            validate_grant(scopes, profile_ids)
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        if not store.pop('pending', store.digest(transaction)):
            return JSONResponse({'error': 'Consent already completed'}, status_code=400)
        code, grant = secrets.token_urlsafe(32), secrets.token_hex(16)
        record = MCPCode(code='', client_id=pending['client_id'], scopes=scopes, profile_ids=profile_ids,
                         grant_id=grant, expires_at=time.time() + 120, code_challenge=params['code_challenge'],
                         redirect_uri=params['redirect_uri'], redirect_uri_provided_explicitly=params['redirect_uri_provided_explicitly'],
                         resource=self.resource, subject='admin').model_dump(mode='json')
        record.pop('code')
        store.put('code', store.digest(code), record)
        store.audit('admin', 'oauth_authorize', 'completed', result={'client_id': pending['client_id'], 'scopes': scopes, 'profile_ids': profile_ids})
        response = RedirectResponse(construct_redirect_uri(params['redirect_uri'], code=code, state=params.get('state')), status_code=303)
        response.delete_cookie('mcp_consent', path='/oauth/consent')
        response.headers['Cache-Control'] = 'no-store'
        return response
