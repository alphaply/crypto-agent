from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import store
from backend.utils.spot_config_guard import serialized_spot_execution

SCOPES = ['read', 'trade', 'cancel']


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
    symbols: list[str] = Field(min_length=1, max_length=30)
    enabled: bool = True
    max_leverage: int = Field(5, ge=1, le=125)
    leverage: int = Field(1, ge=1, le=125)
    exit_mode: Literal['attached_required', 'attached_optional', 'independent_exits'] = 'attached_required'
    spot_allowance: float = Field(100, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_policy(self):
        if self.leverage > self.max_leverage:
            raise ValueError('Configured leverage exceeds the MCP maximum')
        if self.market_type == 'spot':
            from backend.utils.spot_portfolio import normalize_spot_symbols
            normalize_spot_symbols(self.symbols)
        return self

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
    return store.list_records('profile')


def exchange_profile(profile_id):
    from backend.config_store import load_effective_runtime_snapshot
    result = next((row for row in load_effective_runtime_snapshot().get('exchange_profiles', []) if row['profile_id'] == profile_id), None)
    if not result:
        raise ValueError('MCP exchange profile no longer exists')
    return result


@serialized_spot_execution
def save_profile(payload: MCPProfile):
    if payload.leverage > payload.max_leverage:
        raise ValueError('Configured leverage exceeds the MCP maximum')
    account = exchange_profile(payload.exchange_profile_id)
    if account.get('market_type', 'swap') != payload.market_type:
        raise ValueError('MCP market type must match its exchange account')
    if payload.market_type == 'spot':
        from backend.utils.spot_portfolio import normalize_spot_symbols
        normalize_spot_symbols(payload.symbols)
    previous = store.get('profile', payload.profile_id)
    from .config_guard import assert_profile_transition
    with store.connection() as conn:
        assert_profile_transition(conn, previous, payload.model_dump())
    if previous and any(previous.get(key) != getattr(payload, key) for key in ('exchange_profile_id', 'market_type')):
        # Persistent order ownership cannot silently move to a different account.
        with store.connection() as conn:
            used = conn.execute('SELECT 1 FROM mcp_operations WHERE profile_id=? LIMIT 1', (payload.profile_id,)).fetchone()
        if used:
            raise ValueError('This profile has execution history; create a new profile for another account')
    store.put('profile', payload.profile_id, payload.model_dump())
    return payload.model_dump()


def maintenance_profiles():
    """Include paused profiles: previously submitted orders still need protection."""
    return [get_runtime_profile('mcp:' + row['profile_id']) for row in profiles()]


def get_runtime_profile(config_id: str):
    """Root Config resolves these on demand; never include them in scheduler configs."""
    if not str(config_id).startswith('mcp:'):
        return None
    profile = store.get('profile', config_id[4:])
    if not profile:
        return None
    account = exchange_profile(profile['exchange_profile_id'])
    return {
        **account, 'config_id': config_id, 'title': profile['name'],
        'exchange_profile_id': profile['exchange_profile_id'], 'symbol': profile['symbols'][0],
        'symbols': profile['symbols'], 'market_type': profile['market_type'],
        'mode': 'SPOT_DCA' if profile['market_type'] == 'spot' else 'REAL',
        'enabled': profile['enabled'], 'mcp_profile': True, 'mcp_max_leverage': profile['max_leverage'],
        'leverage': profile['leverage'], 'exit_mode': profile['exit_mode'],
        'dca_amount': profile['spot_allowance'], 'dca_freq': '1d', 'dca_time': '00:00',
        'market_timeframes': ['1h', '4h', '1d'],
    }
