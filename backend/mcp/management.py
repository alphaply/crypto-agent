from __future__ import annotations

import json
import time
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from backend.app.core.deps import get_current_user
from . import store
from .auth import admin_keys, admin_connections, create_api_key
from backend.utils.spot_config_guard import serialized_spot_execution
from .settings import MCPSettings, MCPProfile, SCOPES, profiles, save_profile, settings
from .connection import ChatGPTClientRequest, check_connection, connection_details, register_chatgpt_client
from . import paper
from typing import Literal

def prevent_caching(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/api/mcp', tags=['mcp'], dependencies=[Depends(get_current_user), Depends(prevent_caching)])


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ['read'])
    profile_ids: list[str] = Field(default_factory=list)
    key_type: Literal['live', 'paper'] = 'live'
    paper_namespace: str = 'qqbot'


@router.get('')
def overview(response: Response):
    from .server import active_public_url, tool_catalog
    from .resilience import diagnostics
    value = settings()
    url = value['public_url']
    response.headers['Cache-Control'] = 'no-store'
    return {'success': True, 'settings': value, 'profiles': profiles(), 'keys': admin_keys(), 'oauth_connections': admin_connections(),
            'tools': tool_catalog(), 'scopes': SCOPES, 'audit': store.audit_rows(50),
            'connection': connection_details(url, active_public_url()), 'runtime_diagnostics': diagnostics(),
            'paper_settings': paper.settings()}


@router.get('/connection-check')
async def connection_check():
    from .server import active_public_url
    return await check_connection(settings()['public_url'], active_public_url())


@router.post('/oauth-clients')
async def create_oauth_client(payload: ChatGPTClientRequest):
    from .server import active_public_url
    value = settings()
    connection = connection_details(value['public_url'], active_public_url())
    if connection['restart_required']:
        raise HTTPException(409, '公开地址尚未生效，请重启后端后再生成客户端')
    try:
        client = await register_chatgpt_client(payload, value['public_url'])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'success': True, 'client': client, 'connection': connection}


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
        conn.execute('BEGIN IMMEDIATE')
        used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=? LIMIT 1', (profile_id,)).fetchone()
        if used:
            raise HTTPException(409, 'This profile has execution history; disable it instead to retain order ownership and receipts')
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id=?", (profile_id,)).fetchone()
        previous = json.loads(row[0]) if row else None
        if previous:
            from .config_guard import active_lifecycle
            if active_lifecycle(conn, previous):
                raise HTTPException(409, 'This profile has active exposure or unresolved operations; disable decisions while retaining its management scope')
        conn.execute("DELETE FROM mcp_records WHERE kind='profile' AND id=?", (profile_id,))
        if (previous or {}).get('position_management_policy') == 'exclusive_scope':
            from backend.config_store import load_runtime_snapshot
            from backend.utils.position_management import synchronize_configuration
            snapshot = load_runtime_snapshot(connection=conn)
            if snapshot is None:
                raise HTTPException(409, 'Unable to verify management ownership without its saved runtime account configuration')
            try:
                synchronize_configuration(conn, snapshot)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        conn.commit()
    return {'success': True}


@router.post('/keys')
def add_key(payload: KeyRequest, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        value = create_api_key(payload.name, payload.scopes, payload.profile_ids, payload.key_type, payload.paper_namespace)
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


@router.put('/paper/settings')
def update_paper_settings(payload: paper.PaperSettings):
    store.put('paper_settings', 'global', payload.model_dump())
    return {'success': True, 'settings': payload.model_dump()}


@router.get('/paper/accounts')
def list_paper_accounts(namespace: str | None = None, user_id: str | None = None, limit: int = 100, offset: int = 0, cursor: int | None = None):
    try:
        return {'success': True, **paper.admin_accounts(namespace, user_id, limit, offset, cursor=cursor)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/paper/dashboard')
def paper_dashboard(namespace: str | None = None, user_id: str | None = None, limit: int = 100,
                    account_cursor: int = 0, order_cursor: int = 0, event_cursor: int = 0, namespace_cursor: str = ''):
    from .paper_competition import admin_data
    from .paper_orders import admin_orders
    try:
        if not 1 <= limit <= 200:
            raise ValueError('limit must be 1–200')
        if namespace:
            paper.identity(namespace, 'namespace')
        if user_id:
            paper.identity(user_id)
        accounts = paper.admin_accounts(namespace, user_id, limit, cursor=account_cursor)
        data = admin_data(namespace, user_id, limit, cursor=event_cursor, namespace_cursor=namespace_cursor)
        orders = admin_orders(namespace, user_id, 'all', limit, cursor=order_cursor)
        components = {'accounts': {key: accounts[key] for key in ('status', 'scan_complete', 'next_cursor', 'skipped_records')},
                      'orders': {key: orders[key] for key in ('status', 'scan_complete', 'next_cursor', 'skipped_records')},
                      'events': {key: data[key] for key in ('status', 'scan_complete', 'next_cursor', 'skipped_records')},
                      'namespaces': data['namespaces'], 'rankings': data['rankings']}
        scan_complete = all(row['scan_complete'] for row in components.values() if isinstance(row, dict)) and all(row['scan_complete'] for row in data['rankings'])
        partial = any(row['status'] != 'complete' for row in components.values() if isinstance(row, dict)) or any(row['status'] != 'complete' for row in data['rankings'])
        complete_equity = accounts['status'] == 'complete' and all(a['net_equity'] is not None for a in accounts['items'])
        return {'success': True, **data, **orders, 'accounts': accounts['items'], 'account_total': accounts['total'],
                'status': 'partial' if partial else 'complete', 'scan_complete': scan_complete, 'pages': components,
                'next_cursor': {'accounts': accounts['next_cursor'] if accounts['has_more'] else None,
                                'orders': orders['next_cursor'] if orders['has_more'] else None,
                                 'events': data['next_cursor'] if data['has_more'] else None,
                                 'namespaces': data['namespaces']['next_cursor']},
                'summary': {'cash': sum(a['cash'] for a in accounts['items']), 'debt': sum(a['debt'] for a in accounts['items']),
                            'reserved_cash': sum(a.get('reserved_cash', 0) for a in accounts['items']),
                            'used_margin': sum(a.get('used_margin', 0) for a in accounts['items']),
                            'available_cash': sum(a['available_cash'] for a in accounts['items']),
                            'net_equity': sum(a['net_equity'] for a in accounts['items']) if complete_equity else None,
                            'valuation_status': 'complete' if complete_equity else 'incomplete',
                            'open_orders': sum(o['state'] == 'open' for o in orders['orders']),
                            'summary_scope': 'returned_accounts_and_orders', 'account_count': len(accounts['items'])}}
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/paper/receipts/{namespace}/{user_id}/{operation_id}')
def paper_receipt_audit(namespace: str, user_id: str, operation_id: str, offset: int = 0, length: int = 16384, version: str | None = None, view: str = 'original', encoding: str = 'base64-utf8'):
    """JWT administrator audit; binary chunks preserve immutable original bytes."""
    from .paper_storage import audit_chunk
    try:
        paper.identity(namespace, 'namespace')
        paper.identity(user_id)
        with store.connection() as conn:
            return {'success': True, **audit_chunk(conn, namespace, user_id, operation_id, offset=offset, length=length, version=version, view=view, encoding=encoding)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/paper/accounts/{namespace}/{user_id}/audit')
def paper_account_audit(namespace: str, user_id: str, offset: int = 0, length: int = 16384, version: str | None = None, encoding: str = 'base64-utf8'):
    return paper_ledger_audit(namespace, user_id, 'account', user_id, offset, length, version, encoding)


@router.get('/paper/ledger/{namespace}/{user_id}/{record_type}/{record_id}/audit')
def paper_ledger_audit(namespace: str, user_id: str, record_type: str, record_id: str,
                       offset: int = 0, length: int = 16384, version: str | None = None, encoding: str = 'base64-utf8'):
    from .paper_storage import raw_audit_chunk
    try:
        paper.identity(namespace, 'namespace')
        paper.identity(user_id)
        with store.connection(recovery=True) as conn:
            return {'success': True, **raw_audit_chunk(conn, namespace, user_id, record_type, record_id,
                                                      offset=offset, length=length, version=version, encoding=encoding)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/paper/accounts/{namespace}/{user_id}')
def create_paper_account_admin(namespace: str, user_id: str):
    caller = {'id': 'admin', 'key_type': 'paper', 'paper_namespace': namespace, 'scopes': ['read', 'trade']}
    try:
        return {'success': True, **paper.create_account(caller, user_id, 'admin-create-' + store.digest(namespace + ':' + user_id)[:24])}
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch('/paper/accounts/{namespace}/{user_id}')
def update_paper_account(namespace: str, user_id: str, payload: paper.AccountUpdate):
    try:
        return {'success': True, 'account': paper.admin_update(namespace, user_id, payload)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
