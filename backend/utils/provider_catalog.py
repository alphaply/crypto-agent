"""Bounded read-only model catalogs shared by configuration and Jev discovery."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx


TOTAL_TIMEOUT = 15.0
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 10
MAX_MODELS = 5000
JEV_MODEL_ID = re.compile(r"jev-[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}\Z")
ISSUES = {
    "invalid_connection": ("Enter a valid HTTP(S) API Base without credentials, query or fragment.", False),
    "missing_credentials": ("Enter an API key for this connection.", False),
    "provider_not_found": ("The saved provider no longer exists.", False),
    "connection_changed": ("The saved connection has changed. Enter a key for the current connection.", False),
    "credentials_rejected": ("The supplier rejected the API key or model-list permission.", False),
    "catalog_not_supported": ("The supplier does not expose this model-list endpoint. Enter a model manually.", False),
    "rate_limited": ("The supplier rate limited this request. Refresh later.", True),
    "timeout": ("Model discovery timed out. Refresh later or enter a model manually.", True),
    "connection_error": ("Could not read the supplier model list.", True),
    "upstream_error": ("The supplier could not return its model list.", True),
    "invalid_response": ("The supplier returned an invalid model list.", False),
    "response_too_large": ("The supplier model-list response exceeded the size limit.", False),
    "catalog_limit": ("Only part of the model list was read because the catalog limit was reached.", False),
    "pagination_error": ("The supplier returned an unusable model-list cursor.", False),
    "pagination_unsupported": ("The supplier uses an unsupported model-list pagination format.", False),
}


class CatalogError(ValueError):
    def __init__(self, code: str, status: int = 502):
        message, retryable = ISSUES[code]
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = {"code": code, "message": message, "retryable": retryable}


def normalize_base(value: str) -> str:
    try:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError
        base = value.strip().rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in base or "#" in base or "\\" in base or " " in base):
            raise ValueError
        port = parsed.port  # Validate malformed ports even when they are not used.
        host = parsed.hostname.lower()
        host = f"[{host}]" if ":" in host else host
        authority = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path, "", ""))
    except (ValueError, TypeError):
        raise CatalogError("invalid_connection", 400) from None


def connection_identity(provider: dict) -> tuple:
    protocol = provider.get("api_protocol") or "chat"
    return (normalize_base(provider.get("api_base") or ""), protocol,
            provider.get("compatibility_mode") or "auto",
            (provider.get("decisions_api") or "bai") if protocol == "decisions" else None)


def catalog_target(provider: dict) -> tuple[str, str, dict]:
    from backend.utils.llm_utils import (
        _anthropic_base_url, _provider_default_headers, resolve_compatibility_mode,
    )
    base = normalize_base(provider.get("api_base") or "")
    key = provider.get("api_key") or ""
    if not key.strip() or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise CatalogError("missing_credentials", 400)
    # Source URLs are returned to the configuration UI. A credential embedded
    # in that address must never become a successful source field or request
    # path; authentication belongs exclusively in the adapter's headers.
    if key in base or key in unquote(urlsplit(base).path):
        raise CatalogError("invalid_connection", 400)
    headers = {"User-Agent": "crypto-agent/0.2", **(_provider_default_headers(base) or {})}
    if provider.get("api_protocol") == "decisions":
        from backend.utils.jev import api_base, validate_connection
        validate_connection(provider)
        mode, url = "decisions", api_base({**provider, "api_base": base}) + "/models"
    else:
        mode = resolve_compatibility_mode(provider.get("compatibility_mode"), model=None, base_url=base)
        url = (_anthropic_base_url(base) + "/v1/models") if mode == "anthropic" else base + "/models"
    if mode == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {key}"
    return mode, url, headers


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=5.0), follow_redirects=False)


async def _read_page(client: httpx.AsyncClient, url: str, headers: dict, params: dict) -> dict:
    async with client.stream("GET", url, headers=headers, params=params) as response:
        status = response.status_code
        if status in {401, 403}:
            raise CatalogError("credentials_rejected")
        if status in {404, 405, 501}:
            raise CatalogError("catalog_not_supported")
        if status == 429:
            raise CatalogError("rate_limited")
        if status != 200:
            raise CatalogError("upstream_error")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > MAX_PAGE_BYTES:
                raise CatalogError("response_too_large")
            body.extend(chunk)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise CatalogError("invalid_response") from None
        if not isinstance(payload, dict):
            raise CatalogError("invalid_response")
        return payload


def _model_rows(payload: dict, protocol: str) -> list[dict]:
    values = payload.get("models", payload.get("data")) if protocol == "decisions" else payload.get("data")
    if not isinstance(values, list):
        raise CatalogError("invalid_response")
    rows, valid = [], 0
    for value in values:
        if not isinstance(value, dict):
            continue
        model_id = (value.get("name") or value.get("id")) if protocol == "decisions" else value.get("id")
        if not isinstance(model_id, str) or not model_id.strip() or any(ord(c) < 32 for c in model_id):
            continue
        valid += 1
        if protocol == "decisions" and not JEV_MODEL_ID.fullmatch(model_id):
            continue
        name = model_id if protocol == "decisions" else value.get("display_name") or value.get("name") or model_id
        description = value.get("description") or ""
        rows.append({"id": model_id, "name": name[:500] if isinstance(name, str) else model_id,
                     "description": description[:500] if isinstance(description, str) else ""})
    if values and not valid:
        raise CatalogError("invalid_response")
    return rows


async def fetch_models(provider: dict) -> dict:
    """Return actual observations only; never save, infer, retry or follow redirects."""
    protocol, url, headers = catalog_target(provider)
    found: dict[str, dict] = {}
    pages = 0
    warning = None

    async def collect():
        nonlocal pages, warning
        cursor = None
        seen_cursors = set()
        async with _client() as client:
            for _ in range(MAX_PAGES):
                params = {"after_id": cursor} if cursor else {}
                payload = await _read_page(client, url, headers, params)
                rows = _model_rows(payload, protocol)
                # An upstream echo must not put the credential into candidate metadata.
                if any(provider["api_key"] in field for row in rows for field in row.values()):
                    raise CatalogError("invalid_response")
                for row in rows:
                    if row["id"] not in found and len(found) >= MAX_MODELS:
                        warning = CatalogError("catalog_limit").detail
                        return
                    found.setdefault(row["id"], row)
                pages += 1
                has_more = payload.get("has_more", False)
                if not isinstance(has_more, bool):
                    raise CatalogError("pagination_error")
                if not has_more:
                    if payload.get("next") or payload.get("next_page") or payload.get("next_cursor"):
                        warning = CatalogError("pagination_unsupported").detail
                    return
                if protocol != "anthropic":
                    warning = CatalogError("pagination_unsupported").detail
                    return
                cursor = payload.get("last_id")
                if (not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen_cursors
                        or provider["api_key"] in cursor or any(ord(char) < 32 or ord(char) == 127 for char in cursor)):
                    raise CatalogError("pagination_error")
                seen_cursors.add(cursor)
            warning = CatalogError("catalog_limit").detail

    try:
        await asyncio.wait_for(collect(), timeout=TOTAL_TIMEOUT)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        error = CatalogError("timeout", 504)
        if not pages:
            raise error from None
        warning = error.detail
    except Exception as exc:
        error = exc if isinstance(exc, CatalogError) else CatalogError("connection_error")
        if not pages:
            raise error from None
        warning = error.detail
    return {"success": True, "models": [found[key] for key in sorted(found)],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": {"api_base": normalize_base(provider["api_base"]), "url": url, "protocol": protocol},
            "complete": warning is None, "warning": warning}
