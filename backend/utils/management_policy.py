"""Explicit finite management authorization, separate from query visibility."""
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
    if policy == 'exclusive_scope':
        policy = 'visible_first_claim'
    result['position_management_policy'] = policy
    if policy == 'visible_first_claim':
        result['management_scopes'] = []
        result['management_policy_version'] = 2
        return result
    if policy != 'exclusive_scope':
        if result.get('management_scopes'):
            raise ValueError('Management scopes require exclusive_scope policy')
        return result
    if (str(result.get('mode') or 'REAL').upper() != 'REAL'
            or (result.get('market_type') or 'swap') != 'swap'
            or result.get('exit_mode') != 'independent_exits'):
        raise ValueError('Exclusive management requires REAL swap independent_exits')
    raw = result.get('management_scopes')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise ValueError('Choose 1–30 finite management scopes')
    scopes = [ManagementScope.model_validate(item).model_dump() for item in raw]
    if len({(item['symbol'], item['side']) for item in scopes}) != len(scopes):
        raise ValueError('Duplicate management scope')
    allowed = result.get('symbols') or ([result['symbol']] if result.get('symbol') else [])
    if result.get('symbol_scope', 'selected') != 'all' and allowed:
        symbols = {canonical_symbol(s) for s in allowed}
        if any(item['symbol'] not in symbols for item in scopes):
            raise ValueError('Management scope is outside the task/profile symbols')
    result['management_scopes'] = scopes
    result['position_management_policy'] = policy
    return result


def exclusive(config: dict | None, symbol: str, side: str | None = None) -> bool:
    config = config or {}
    if config.get('position_management_policy') == 'exclusive_scope':
        config = normalize_management(config)
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
    if config.get('position_management_policy') != 'exclusive_scope':
        return False
    canonical = canonical_symbol(symbol)
    return any(item['symbol'] == canonical and (side is None or item['side'] == side)
               for item in normalize_management(config)['management_scopes'])
