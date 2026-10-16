"""Configuration migration; OAuth grants intentionally stay bound to their issuer."""
from __future__ import annotations
from backend.storage import lock_scope

import json
import time

from . import store
from .auth import admin_keys, encrypt
from .settings import MCPSettings, MCPProfile, profiles, settings, SCOPES


def export_settings(include_secrets=True):
    keys = admin_keys()
    if not include_secrets:
        keys = [{key: value for key, value in row.items() if key not in {'secret', 'digest'}} for row in keys]
    from .paper import settings as paper_settings
    return {'settings': settings(), 'profiles': profiles(), 'keys': keys, 'paper_settings': paper_settings()}


def import_settings(payload, connection=None):
    parsed_settings = MCPSettings.model_validate(payload.get('settings', {})).model_dump()
    profile_models = [MCPProfile.model_validate(row) for row in payload.get('profiles', [])]
    parsed_profiles = [row.model_dump() for row in profile_models]
    profile_ids = {row['profile_id'] for row in parsed_profiles}
    if len(profile_ids) != len(parsed_profiles):
        raise ValueError('Duplicate imported MCP profile identity')
    records = [('settings', 'global', parsed_settings)]
    if 'paper_settings' in payload:
        from .paper import PaperSettings
        records.append(('paper_settings', 'global', PaperSettings.model_validate(payload['paper_settings']).model_dump()))
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
        key_type = row.get('key_type', 'live')
        if key_type == 'paper':
            from .paper import identity
            identity(row.get('paper_namespace'), 'paper_namespace')
            if row.get('profile_ids') or not set(row['scopes']) <= {'read', 'trade'}:
                raise ValueError('Imported paper key cannot grant live profiles or cancel scope')
        elif key_type != 'live':
            raise ValueError('Unknown imported MCP key type')
        key_id = str(row.get('id') or '')
        if not key_id or len(key_id) > 100:
            raise ValueError('Invalid imported MCP key identity')
        value = {'id': key_id, 'name': str(row.get('name') or key_id), 'scopes': row['scopes'],
                 'profile_ids': row.get('profile_ids', []), 'created_at': row.get('created_at', time.time()),
                 'revoked': bool(row.get('revoked')), 'secret': encrypt(raw), 'digest': store.digest(raw)}
        value.update(key_type=key_type, paper_namespace=row.get('paper_namespace') if key_type == 'paper' else None)
        records.extend([('key', key_id, value), ('key_lookup', value['digest'], {'id': key_id})])

    def write(conn):
        tables = {row[0] for row in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        accounts = {row['profile_id']: row['market_type'] for row in conn.execute('SELECT profile_id,market_type FROM exchange_profiles')} if 'exchange_profiles' in tables else {}
        managed_transition = False
        imported_profiles = []
        for profile, model in zip(parsed_profiles, profile_models):
            if accounts.get(profile['exchange_profile_id']) != profile['market_type']:
                raise ValueError('Imported MCP profile references a missing or incompatible exchange account')
            previous_row = conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id=%s", (profile['profile_id'],)).fetchone()
            previous = json.loads(previous_row[0]) if previous_row else None
            if previous:
                for field in ('position_management_policy', 'management_scopes', 'exit_mode'):
                    if field not in model.model_fields_set and previous.get(field) is not None:
                        if field == 'management_scopes' and 'position_management_policy' in model.model_fields_set and model.position_management_policy == 'strict_owned':
                            continue
                        profile[field] = previous[field]
                profile = MCPProfile.model_validate(profile).model_dump()
            managed_transition |= profile.get('position_management_policy') == 'exclusive_scope' or (previous or {}).get('position_management_policy') == 'exclusive_scope'
            if previous:
                from .config_guard import assert_profile_transition
                assert_profile_transition(conn, previous, profile)
                used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=%s LIMIT 1', (profile['profile_id'],)).fetchone() if 'mcp_operations' in tables else None
                if used and any(previous.get(key) != profile[key] for key in ('exchange_profile_id', 'market_type')):
                    raise ValueError('Cannot import a different account over an MCP profile with trading history')
            imported_profiles.append(('profile', profile['profile_id'], profile))
        for kind, key, value in [*records, *imported_profiles]:
            if kind == 'paper_settings':
                from .paper_lifecycle import record_rate_change
                record_rate_change(conn, value)
            conn.execute('INSERT INTO mcp_records(kind,id,payload) VALUES(%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',
                         (kind, key, json.dumps(value, ensure_ascii=False)))
        if managed_transition:
            from backend.config_store import load_runtime_snapshot
            from backend.utils.position_management import synchronize_configuration
            snapshot = load_runtime_snapshot(connection=conn)
            if snapshot is None:
                raise ValueError('Exclusive management import requires saved exchange accounts and runtime configuration')
            synchronize_configuration(conn, snapshot)
    # Initialize lazily before joining a caller's transaction.
    if connection is not None:
        # Table installed by the explicit PostgreSQL migration.
        write(connection)
    else:
        with store.connection() as conn:
            lock_scope(conn, 'runtime-config')
            write(conn)
            conn.commit()
