"""Provider-specific prices and cached models.dev synchronization."""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.app.schemas.news import PricingSyncSettings
from backend import database_news

CATALOG_URL = 'https://models.dev/api.json'
RATE_FIELDS = ('input_price_per_m', 'output_price_per_m', 'cache_read_price_per_m', 'cache_write_price_per_m')


def _initialize(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS provider_model_pricing (
        provider_id TEXT NOT NULL, model TEXT NOT NULL, payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL, PRIMARY KEY(provider_id, model))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS model_catalog_cache (
        id INTEGER PRIMARY KEY CHECK(id=1), fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL)''')


def _runtime() -> dict:
    from backend.config_store import load_effective_runtime_snapshot
    return load_effective_runtime_snapshot()


def _amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def save_provider_pricing(provider: dict) -> None:
    """Save only explicitly entered rates; automatic catalog rates live separately."""
    if not provider.get('provider_id') or not provider.get('model'):
        return
    if provider.get('pricing_mode', 'manual') == 'models_dev':
        return
    values = {key: _amount(provider.get(key)) for key in RATE_FIELDS}
    values.update(currency=provider.get('pricing_currency') or 'USD', source='manual')
    _save_rates(provider['provider_id'], provider['model'], values)


def _save_rates(provider_id: str, model: str, values: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        _initialize(conn)
        conn.execute('INSERT INTO provider_model_pricing VALUES (?,?,?,?) ON CONFLICT(provider_id,model) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at', (provider_id, model, json.dumps(values), database_news.now_iso()))
        conn.commit()


def resolve_provider_id(model: str, config_id: str = '', purpose: str = '', *, base_url: str | None = None) -> str | None:
    """Resolve only unambiguous legacy calls; never attribute by model alone if duplicated."""
    try:
        snapshot = _runtime()
        providers = snapshot.get('llm_providers', [])
        candidates = [p for p in providers if p.get('model') == model]
        if base_url:
            candidates = [p for p in candidates if str(p.get('api_base') or '').rstrip('/') == base_url.rstrip('/')]
        agent = next((a for a in snapshot.get('agents', []) if a.get('config_id') == config_id), {})
        if agent and purpose in {'strategy_summary', 'daily_summary', 'memory_review', 'chat_summary'}:
            from backend.utils.llm_utils import resolve_summarizer_provider_id
            return resolve_summarizer_provider_id(agent)
        key = 'summarizer_provider_id' if purpose in {'strategy_summary', 'daily_summary', 'memory_review', 'chat_summary'} else 'llm_provider_id'
        requested = agent.get(key)
        if requested and any(p['provider_id'] == requested for p in candidates):
            return requested
        return str(candidates[0]['provider_id']) if len(candidates) == 1 else None
    except Exception:
        return None


def get_price_snapshot(model: str, provider_id: str | None = None, *, conn=None) -> dict:
    """Return a copy of current rates; callers persist it before a model request."""
    from backend.database import get_db_conn
    from contextlib import nullcontext
    provider = None
    if not provider_id:
        try:
            if any(p.get('model') == model for p in _runtime().get('llm_providers', [])):
                # A channel exists but attribution was ambiguous or did not match
                # the actual connection. Model-only legacy prices cannot price it.
                return {**{key: None for key in RATE_FIELDS}, 'currency': 'USD', 'source': None}
        except Exception:
            pass
    if provider_id:
        try:
            provider = next((p for p in _runtime().get('llm_providers', []) if p['provider_id'] == provider_id and p.get('model') == model), None)
        except Exception:
            pass
        if provider and provider.get('pricing_mode', 'manual') != 'models_dev':
            return {**{key: _amount(provider.get(key)) for key in RATE_FIELDS}, 'currency': provider.get('pricing_currency') or 'USD', 'source': provider.get('pricing_source') or 'manual'}
    with (nullcontext(conn) if conn is not None else get_db_conn()) as db:
        _initialize(db)
        if provider_id:
            row = db.execute('SELECT payload_json FROM provider_model_pricing WHERE provider_id=? AND model=?', (provider_id, model)).fetchone()
            if row:
                data = json.loads(row[0])
                if not provider or data.get('source') == 'models_dev':
                    return data
            return {**{key: None for key in RATE_FIELDS}, 'currency': 'USD', 'source': None}
        try:
            row = db.execute('SELECT input_price_per_m,output_price_per_m,currency FROM model_pricing WHERE model=?', (model,)).fetchone()
        except Exception:
            row = None
        if row:
            return {'input_price_per_m': _amount(row[0]), 'output_price_per_m': _amount(row[1]), 'currency': row[2] or 'USD', 'source': 'legacy'}
    return {**{key: None for key in RATE_FIELDS}, 'currency': 'USD', 'source': None}


def get_catalog(*, force: bool = False) -> dict:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        _initialize(conn)
        row = conn.execute('SELECT * FROM model_catalog_cache WHERE id=1').fetchone()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if row and row['fetched_at'] > cutoff and not force:
        return {'data': json.loads(row['payload_json']), 'fetched_at': row['fetched_at'], 'stale': False}
    try:
        response = httpx.get(CATALOG_URL, timeout=20, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > 30 * 1024 * 1024:
            raise ValueError('Model catalog exceeds the size limit')
        data = response.json()
        if not isinstance(data, dict) or not any(isinstance(v, dict) and isinstance(v.get('models'), dict) for v in data.values()):
            raise ValueError('Invalid models.dev catalog')
        fetched = database_news.now_iso()
        with get_db_conn() as conn:
            _initialize(conn)
            conn.execute('INSERT OR REPLACE INTO model_catalog_cache VALUES (1,?,?)', (fetched, json.dumps(data)))
            conn.commit()
        return {'data': data, 'fetched_at': fetched, 'stale': False}
    except Exception:
        if row:
            return {'data': json.loads(row['payload_json']), 'fetched_at': row['fetched_at'], 'stale': True, 'error': 'models.dev is unavailable; showing the cached catalog'}
        raise


def catalog_payload(query: str = '', limit: int = 100) -> dict:
    result = get_catalog()
    items = []
    for provider_id, provider in result['data'].items():
        if not isinstance(provider, dict):
            continue
        for model_id, model in (provider.get('models') or {}).items():
            if not isinstance(model, dict) or model.get('status') == 'deprecated':
                continue
            outputs = (model.get('modalities') or {}).get('output')
            if outputs and 'text' not in outputs:
                continue
            if query.lower() not in f"{provider_id} {model_id} {model.get('name', '')}".lower():
                continue
            items.append({'provider_id': provider_id, 'provider_name': provider.get('name', provider_id), 'model_id': model_id, 'name': model.get('name', model_id), 'cost': model.get('cost'), 'canonical_model_id': model.get('canonical_model_id')})
    return {key: value for key, value in {**result, 'data': None, 'models': items[:limit], 'total': len(items)}.items() if key != 'data'}


def sync_prices(*, force: bool = True, owner: str | None = None) -> dict:
    owner = owner or database_news.acquire_lease('pricing', seconds=120)
    if not owner:
        return {**database_news.read_state('pricing'), 'started': False}
    database_news.update_state('pricing', {'last_attempt_at': database_news.now_iso(), 'status': 'running'}, owner=owner)
    try:
        catalog = get_catalog(force=force)
        if catalog['stale']:
            raise ValueError('models.dev is unavailable; existing prices were retained')
        changed, unmatched = 0, []
        for provider in _runtime().get('llm_providers', []):
            if provider.get('pricing_mode', 'manual') != 'models_dev':
                continue
            catalog_provider = provider.get('models_dev_provider_id')
            catalog_model = provider.get('models_dev_model_id')
            model = ((catalog['data'].get(catalog_provider) or {}).get('models') or {}).get(catalog_model)
            cost = model.get('cost') if isinstance(model, dict) else None
            if not isinstance(cost, dict) or _amount(cost.get('input')) is None or _amount(cost.get('output')) is None:
                unmatched.append(provider['provider_id'])
                continue
            values = {'input_price_per_m': _amount(cost.get('input')), 'output_price_per_m': _amount(cost.get('output')), 'cache_read_price_per_m': _amount(cost.get('cache_read')), 'cache_write_price_per_m': _amount(cost.get('cache_write')), 'currency': 'USD', 'source': 'models_dev', 'catalog_provider_id': catalog_provider, 'catalog_model_id': catalog_model, 'synced_at': catalog['fetched_at'], 'tiers': cost.get('tiers') or ([] if not cost.get('context_over_200k') else [{'tier': {'size': 200000}, **cost['context_over_200k']}])}
            _save_rates(provider['provider_id'], provider['model'], values)
            changed += 1
        result = {'status': 'success', 'last_success_at': database_news.now_iso(), 'updated': changed, 'unmatched_provider_ids': unmatched, 'error': None}
    except Exception as exc:
        result = {'status': 'error', 'error': str(exc) if isinstance(exc, ValueError) else 'models.dev synchronization failed; existing prices were retained'}
    database_news.update_state('pricing', result, owner=owner, release=True)
    return {**database_news.read_state('pricing'), **result}


def tick_price_sync() -> bool:
    settings = PricingSyncSettings.model_validate(_runtime().get('pricing_sync') or {})
    if not settings.enabled or not any(p.get('pricing_mode') == 'models_dev' for p in _runtime().get('llm_providers', [])):
        return False
    before = (datetime.now(timezone.utc) - timedelta(seconds=settings.interval_seconds)).isoformat()
    owner = database_news.acquire_lease('pricing', seconds=120, due_before=before)
    if not owner:
        return False
    threading.Thread(target=lambda: sync_prices(force=False, owner=owner), name='model-price-sync', daemon=True).start()
    return True
