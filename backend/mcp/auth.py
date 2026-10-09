"""SDK OAuth 2.1 provider; SDK handlers validate PKCE, redirects and client auth."""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
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
        csrf = secrets.token_urlsafe(32)
        store.put('pending', store.digest(transaction), {
            'client_id': client.client_id, 'client_name': client.client_name or client.client_id,
            'params': params.model_dump(mode='json'), 'expires_at': time.time() + 600,
            'csrf': store.digest(csrf), 'csrf_token': encrypt(csrf),
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
        from .consent import handle_consent
        return await handle_consent(self, request)
