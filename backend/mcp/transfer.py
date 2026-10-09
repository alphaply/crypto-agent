"""Configuration migration; OAuth grants intentionally stay bound to their issuer."""
from __future__ import annotations

import json
import time

from . import store
from .auth import admin_keys, encrypt
from .settings import MCPSettings, MCPProfile, profiles, settings, SCOPES


def export_settings(include_secrets=True):
    keys = admin_keys()
    if not include_secrets:
        keys = [{key: value for key, value in row.items() if key not in {'secret', 'digest'}} for row in keys]
    return {'settings': settings(), 'profiles': profiles(), 'keys': keys}


def import_settings(payload, connection=None):
    parsed_settings = MCPSettings.model_validate(payload.get('settings', {})).model_dump()
    parsed_profiles = [MCPProfile.model_validate(row).model_dump() for row in payload.get('profiles', [])]
    profile_ids = {row['profile_id'] for row in parsed_profiles}
    records = [('settings', 'global', parsed_settings)] + [('profile', row['profile_id'], row) for row in parsed_profiles]
    for row in payload.get('keys', []):
        raw = str(row.get('secret') or '')
        if not raw:  # A redacted export must not revoke or replace existing credentials.
            continue
        if not raw.startswith('ca_mcp_') or len(raw) < 40:
            raise ValueError('Invalid imported MCP API key')
        if not set(row.get('scopes', [])) <= set(SCOPES) or 'read' not in row.get('scopes', []):
            raise ValueError('Invalid imported MCP scopes')
        if not set(row.get('profile_ids', [])) <= profile_ids:
            raise ValueError('Imported MCP key references an unknown profile')
        key_id = str(row.get('id') or '')
        if not key_id or len(key_id) > 100:
            raise ValueError('Invalid imported MCP key identity')
        value = {'id': key_id, 'name': str(row.get('name') or key_id), 'scopes': row['scopes'],
                 'profile_ids': row.get('profile_ids', []), 'created_at': row.get('created_at', time.time()),
                 'revoked': bool(row.get('revoked')), 'secret': encrypt(raw), 'digest': store.digest(raw)}
        records.extend([('key', key_id, value), ('key_lookup', value['digest'], {'id': key_id})])

    def write(conn):
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        accounts = {row['profile_id']: row['market_type'] for row in conn.execute('SELECT profile_id,market_type FROM exchange_profiles')} if 'exchange_profiles' in tables else {}
        for profile in parsed_profiles:
            if accounts.get(profile['exchange_profile_id']) != profile['market_type']:
                raise ValueError('Imported MCP profile references a missing or incompatible exchange account')
            previous_row = conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id=?", (profile['profile_id'],)).fetchone()
            if previous_row and 'mcp_operations' in tables:
                previous = json.loads(previous_row[0])
                from .config_guard import assert_profile_transition
                assert_profile_transition(conn, previous, profile)
                used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=? LIMIT 1', (profile['profile_id'],)).fetchone()
                if used and any(previous.get(key) != profile[key] for key in ('exchange_profile_id', 'market_type')):
                    raise ValueError('Cannot import a different account over an MCP profile with trading history')
        for kind, key, value in records:
            conn.execute('INSERT INTO mcp_records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',
                         (kind, key, json.dumps(value, ensure_ascii=False)))
    # Initialize lazily before joining a caller's transaction.
    if connection is not None:
        connection.execute('CREATE TABLE IF NOT EXISTS mcp_records(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id))')
        write(connection)
    else:
        with store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            write(conn)
            conn.commit()
