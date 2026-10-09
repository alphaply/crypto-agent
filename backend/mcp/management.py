from __future__ import annotations

import time
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from backend.app.core.deps import get_current_user
from . import store
from .auth import admin_keys, admin_connections, create_api_key
from backend.utils.spot_config_guard import serialized_spot_execution
from .settings import MCPSettings, MCPProfile, SCOPES, profiles, save_profile, settings

def prevent_caching(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/api/mcp', tags=['mcp'], dependencies=[Depends(get_current_user), Depends(prevent_caching)])


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ['read'])
    profile_ids: list[str] = Field(default_factory=list)


@router.get('')
def overview(response: Response):
    from .server import active_public_url, tool_catalog
    value = settings()
    url = value['public_url']
    response.headers['Cache-Control'] = 'no-store'
    return {'success': True, 'settings': value, 'profiles': profiles(), 'keys': admin_keys(), 'oauth_connections': admin_connections(),
            'tools': tool_catalog(), 'scopes': SCOPES, 'audit': store.audit_rows(50),
            'connection': {'url': url + '/mcp', 'transport': 'streamable-http',
                           'oauth_issuer': url + '/oauth', 'oauth_metadata': url + '/.well-known/oauth-authorization-server/oauth',
                           'resource_metadata': url + '/.well-known/oauth-protected-resource/mcp',
                           'restart_required': bool(active_public_url() and active_public_url() != url),
                           'configuration': {'mcpServers': {'crypto-agent': {'url': url + '/mcp', 'headers': {'Authorization': 'Bearer <API_KEY>'}}}}}}


@router.put('/settings')
def update_settings(payload: MCPSettings):
    store.put('settings', 'global', payload.model_dump())
    return {'success': True, 'settings': payload.model_dump()}


@router.put('/profiles/{profile_id}')
def update_profile(profile_id: str, payload: MCPProfile):
    if payload.profile_id != profile_id:
        raise HTTPException(400, 'Profile ID mismatch')
    try:
        value = save_profile(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'success': True, 'profile': value}


@router.delete('/profiles/{profile_id}')
@serialized_spot_execution
def delete_profile(profile_id: str):
    with store.connection() as conn:
        used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=? LIMIT 1', (profile_id,)).fetchone()
    if used:
        raise HTTPException(409, 'This profile has execution history; disable it instead to retain order ownership and receipts')
    store.pop('profile', profile_id)
    return {'success': True}


@router.post('/keys')
def add_key(payload: KeyRequest, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        value = create_api_key(payload.name, payload.scopes, payload.profile_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'success': True, 'key': value}


@router.delete('/keys/{key_id}')
def revoke_key(key_id: str):
    value = store.get('key', key_id)
    if not value:
        raise HTTPException(404, 'MCP API key not found')
    value['revoked'] = True
    store.put('key', key_id, value)
    return {'success': True}


@router.get('/audit')
def audit():
    return {'success': True, 'items': store.audit_rows()}


@router.delete('/oauth-grants/{grant_id}')
def revoke_grant(grant_id: str):
    if not any(row['id'] == grant_id for row in admin_connections()):
        raise HTTPException(404, 'OAuth connection not found')
    store.put('revoked_grant', grant_id, {'revoked_at': time.time()})
    store.audit('admin', 'oauth_revoke', 'completed', result={'grant_id': grant_id})
    return {'success': True}
