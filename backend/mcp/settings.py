from __future__ import annotations
from backend.storage import lock_scope

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import store
from backend.utils.spot_config_guard import serialized_spot_execution

SCOPES = ['read', 'trade', 'cancel']
_request_symbols: ContextVar[tuple[str, tuple[str, ...]] | None] = ContextVar('mcp_request_symbols', default=None)


@contextmanager
def runtime_symbols(config_id: str, symbols: list[str]):
    """Bind an authorized call's symbols without mutating the saved profile."""
    token = _request_symbols.set((config_id, tuple(symbols)))
    try:
        yield
    finally:
        _request_symbols.reset(token)


class MCPSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    public_url: str = Field(default_factory=lambda: os.getenv('MCP_PUBLIC_URL', 'http://localhost:7860'))

    @field_validator('public_url')
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('public_url must be an absolute HTTP(S) origin')
        if parsed.scheme != 'https' and parsed.hostname not in {'localhost', '127.0.0.1'}:
            raise ValueError('Remote MCP connections require HTTPS')
        if parsed.path not in {'', '/'}:
            raise ValueError('public_url must be an origin without a path')
        return value.rstrip('/')


class MCPProfile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    name: str = Field(min_length=1, max_length=100)
    exchange_profile_id: str = Field(min_length=1, max_length=200)
    market_type: Literal['spot', 'swap'] = 'swap'
    symbol_scope: Literal['selected', 'all'] = 'selected'
    symbols: list[str] = Field(default_factory=list, max_length=30)
    enabled: bool = True
    max_leverage: int = Field(5, ge=1, le=125)
    leverage: int = Field(1, ge=1, le=125)
    exit_mode: Literal['attached_required', 'attached_optional', 'independent_exits'] = 'attached_required'
    position_management_policy: Literal['strict_owned', 'exclusive_scope', 'visible_first_claim'] = 'visible_first_claim'
    management_scopes: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    spot_allowance: float = Field(100, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_policy(self):
        from backend.utils.management_policy import normalize_management
        normalize_management(self.model_dump())
        if self.leverage > self.max_leverage:
            raise ValueError('Configured leverage exceeds the MCP maximum')
        if self.symbol_scope == 'selected' and not self.symbols:
            raise ValueError('Select at least one symbol or explicitly allow all symbols')
        if self.market_type == 'spot' and self.symbol_scope == 'selected':
            from backend.utils.spot_portfolio import normalize_spot_symbols
            normalize_spot_symbols(self.symbols)
        return self

    @field_validator('management_scopes')
    @classmethod
    def valid_management_scopes(cls, scopes):
        from backend.utils.management_policy import ManagementScope
        return [ManagementScope.model_validate(scope).model_dump() for scope in scopes]

    @field_validator('symbols')
    @classmethod
    def valid_symbols(cls, values):
        values = list(dict.fromkeys(value.strip().upper() for value in values))
        if any('/' not in value or len(value) > 50 for value in values):
            raise ValueError('Use exchange symbols such as BTC/USDT or BTC/USDT:USDT')
        return values


def settings():
    return MCPSettings.model_validate(store.get('settings', 'global') or {}).model_dump()


def profiles():
    return [{'symbol_scope': 'selected', **row} for row in store.list_records('profile')]


def exchange_profile(profile_id):
    from backend.config_store import load_effective_runtime_snapshot
    result = next((row for row in load_effective_runtime_snapshot().get('exchange_profiles', []) if row['profile_id'] == profile_id), None)
    if not result:
        raise ValueError('MCP exchange profile no longer exists')
    return result


@serialized_spot_execution
def save_profile(payload: MCPProfile):
    previous = store.get('profile', payload.profile_id)
    if previous:
        updates = payload.model_dump()
        fields = payload.model_fields_set
        for field in ('position_management_policy', 'management_scopes', 'exit_mode'):
            if field not in fields and previous.get(field) is not None:
                if field == 'management_scopes' and 'position_management_policy' in fields and payload.position_management_policy == 'strict_owned':
                    continue
                updates[field] = previous[field]
        payload = MCPProfile.model_validate(updates)
    if payload.leverage > payload.max_leverage:
        raise ValueError('Configured leverage exceeds the MCP maximum')
    account = exchange_profile(payload.exchange_profile_id)
    if account.get('market_type', 'swap') != payload.market_type:
        raise ValueError('MCP market type must match its exchange account')
    if payload.market_type == 'spot' and payload.symbol_scope == 'selected':
        from backend.utils.spot_portfolio import normalize_spot_symbols
        normalize_spot_symbols(payload.symbols)
    from .config_guard import assert_profile_transition
    with store.connection() as conn:
        lock_scope(conn, 'runtime-config')
        current = conn.execute("SELECT payload FROM mcp_records WHERE kind='profile' AND id=%s", (payload.profile_id,)).fetchone()
        current = json.loads(current[0]) if current else None
        if current != previous:
            raise ValueError('MCP profile changed during save; refresh its configuration')
        assert_profile_transition(conn, previous, payload.model_dump())
        if previous and any(previous.get(key) != getattr(payload, key) for key in ('exchange_profile_id', 'market_type')):
            used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=%s LIMIT 1', (payload.profile_id,)).fetchone()
            if used:
                raise ValueError('This profile has execution history; create a new profile for another account')
        conn.execute('INSERT INTO mcp_records(kind,id,payload) VALUES(%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',
                     ('profile', payload.profile_id, json.dumps(payload.model_dump())))
        from backend.config_store import load_runtime_snapshot
        from backend.utils.position_management import synchronize_configuration
        if (payload.position_management_policy == 'exclusive_scope'
                or (previous or {}).get('position_management_policy') == 'exclusive_scope'):
            # Read the candidate account and task config in the same transaction;
            # do not lazily initialize another connection while holding its lock.
            snapshot = load_runtime_snapshot(connection=conn) or {'agents': [], 'exchange_profiles': [account]}
            synchronize_configuration(conn, snapshot)
        conn.commit()
    return payload.model_dump()


def maintenance_profiles():
    """Include paused profiles: previously submitted orders still need protection."""
    result = [get_runtime_profile('mcp:' + row['profile_id']) for row in profiles()]
    return [row for row in result if row and (row.get('symbol') or row.get('enabled') and row.get('position_management_policy') == 'visible_first_claim')]


def get_runtime_profile(config_id: str):
    """Root Config resolves these on demand; never include them in scheduler configs."""
    if not str(config_id).startswith('mcp:'):
        return None
    profile = store.get('profile', config_id[4:])
    if not profile:
        return None
    account = exchange_profile(profile['exchange_profile_id'])
    symbol_scope = profile.get('symbol_scope', 'selected')
    symbols = list(profile.get('symbols') or [])
    owned = []
    if symbol_scope == 'all':
        from .config_guard import owned_symbols
        with store.connection() as conn:
            owned = owned_symbols(conn, profile)
        request = _request_symbols.get()
        symbols = list(request[1]) if request and request[0] == config_id else owned
        symbols = list(dict.fromkeys([*symbols, *[s['symbol'] for s in profile.get('management_scopes') or []]]))
        if profile['market_type'] == 'spot' and symbols:
            # Background readers must not add raw costs of different quote assets.
            # Each gateway call is independently constrained to one quote asset.
            quote = symbols[0].split('/')[1]
            symbols = [symbol for symbol in symbols if symbol.split('/')[1] == quote][:10]
    return {
        **account, 'config_id': config_id, 'title': profile['name'],
        'exchange_profile_id': profile['exchange_profile_id'], 'symbol': symbols[0] if symbols else None,
        'symbols': symbols, 'market_type': profile['market_type'],
        'mcp_symbol_scope': symbol_scope, 'mcp_owned_symbols': owned,
        'mode': 'SPOT_DCA' if profile['market_type'] == 'spot' else 'REAL',
        'enabled': profile['enabled'], 'mcp_profile': True, 'mcp_max_leverage': profile['max_leverage'],
        'leverage': profile['leverage'], 'exit_mode': profile['exit_mode'],
        'position_management_policy': profile.get('position_management_policy', 'visible_first_claim'),
        'management_scopes': profile.get('management_scopes') or [],
        'dca_amount': profile['spot_allowance'], 'dca_freq': '1d', 'dca_time': '00:00',
        'market_timeframes': ['1h', '4h', '1d'],
    }
