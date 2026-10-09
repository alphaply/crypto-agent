"""Prevent exchange/debug payloads and non-finite values escaping the MCP boundary."""
from __future__ import annotations

import math


def sanitize(value, profile_id=None):
    from .settings import get_runtime_profile
    secrets = []
    if profile_id:
        try:
            config = get_runtime_profile('mcp:' + profile_id) or {}
            secrets.extend(str(config[key]) for key in ('api_key', 'secret', 'passphrase', 'password',
                'binance_api_key', 'binance_secret', 'okx_api_key', 'okx_secret', 'okx_passphrase') if config.get(key))
        except Exception:
            pass
    from mcp.server.auth.middleware.auth_context import get_access_token
    token = get_access_token()
    if token:
        secrets.append(token.token)
    sensitive = {'apikey', 'apisecret', 'secret', 'passphrase', 'password', 'authorization',
                 'clientsecret', 'accesstoken', 'refreshtoken', 'signature', 'headers'}
    def clean(item):
        if isinstance(item, dict):
            return {str(key): '[redacted]' if str(key).lower().replace('_', '').replace('-', '') in sensitive else clean(nested)
                    for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(nested) for nested in item]
        if isinstance(item, str):
            for secret in sorted(set(secrets), key=len, reverse=True):
                item = item.replace(secret, '[redacted]')
            return item
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item
    return clean(value)


def safe_error(exc, profile_id=None):
    # HTTP library exceptions can embed signed URLs or arbitrary header dumps.
    if not isinstance(exc, (ValueError, PermissionError)):
        return f'{type(exc).__name__}: exchange or service request failed; query current orders before retrying'
    return sanitize(str(exc), profile_id)
