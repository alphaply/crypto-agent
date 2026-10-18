"""Normalize historical policy names to the trusted shared trading scope."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator


def canonical_symbol(value: str) -> str:
    value = str(value or '').strip().upper()
    if '/' not in value or len(value) > 50 or any(c.isspace() for c in value):
        raise ValueError('Management scope requires a canonical perpetual symbol')
    pair, _, settlement = value.partition(':')
    base, quote = pair.split('/') if pair.count('/') == 1 else ('', '')
    if not base or not quote or (settlement and settlement != quote):
        raise ValueError('Management scope requires a linear perpetual symbol')
    return f'{pair}:{quote}'


class ManagementScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str
    side: Literal['LONG', 'SHORT']

    @field_validator('symbol')
    @classmethod
    def validate_symbol(cls, value):
        return canonical_symbol(value)


def normalize_management(config: dict) -> dict:
    result = dict(config)
    policy = result.get('position_management_policy') or 'visible_first_claim'
    if policy not in {'strict_owned', 'exclusive_scope', 'visible_first_claim'}:
        raise ValueError('Unknown position management policy')
    result['position_management_policy'] = 'visible_first_claim'
    result['management_scopes'] = []
    result['management_policy_version'] = 3
    return result


def exclusive(config: dict | None, symbol: str, side: str | None = None) -> bool:
    config = normalize_management(config or {})
    from backend.utils.visible_management import _active_management
    active = _active_management.get()
    if active and active.get('owner') == config.get('config_id'):
        config = {**config, 'position_management_policy': 'visible_first_claim'}
    if config.get('position_management_policy') == 'visible_first_claim':
        if str(config.get('mode', 'REAL')).upper() != 'REAL' or config.get('market_type', 'swap') != 'swap':
            return False
        allowed = config.get('symbols') or ([config['symbol']] if config.get('symbol') else [])
        return config.get('mcp_symbol_scope', config.get('symbol_scope')) == 'all' or any(
            canonical_symbol(s) == canonical_symbol(symbol) for s in allowed)
    return False
