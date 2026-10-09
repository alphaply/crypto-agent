"""Immutable call-time accounting independent of short-lived raw model traces."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _count(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 and float(value) == parsed else None
    except (ValueError, TypeError, OverflowError):
        return None


def normalize_usage(usage: dict) -> dict:
    inp = _count(usage.get('prompt_tokens', usage.get('input_tokens')))
    out = _count(usage.get('completion_tokens', usage.get('output_tokens')))
    details = usage.get('input_token_details') or usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    output_details = usage.get('output_token_details') or usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
    read = _count(details.get('cache_read', details.get('cached_tokens', usage.get('cache_read_input_tokens')))) or 0
    write = _count(details.get('cache_creation', details.get('cache_write', usage.get('cache_creation_input_tokens')))) or 0
    # Anthropic's native input_tokens excludes cache tokens. LangChain's
    # normalized input_tokens and OpenAI prompt_tokens already include them.
    if inp is not None and 'prompt_tokens' not in usage and not usage.get('input_token_details') and ('cache_read_input_tokens' in usage or 'cache_creation_input_tokens' in usage):
        inp += read + write
    total = _count(usage.get('total_tokens'))
    if inp is not None and out is not None:
        total = inp + out
    return {'prompt_tokens': inp, 'completion_tokens': out, 'total_tokens': total,
            'cache_read_tokens': read, 'cache_write_tokens': write,
            'reasoning_tokens': _count(output_details.get('reasoning', output_details.get('reasoning_tokens'))) or 0}


def calculate_cost(usage: dict, prices: dict) -> tuple:
    from backend.app.services.pricing_service import _amount
    reported = _amount(usage.get('cost'))
    if reported is not None:
        return reported, str(usage.get('currency') or 'USD'), 'provider'
    counts = normalize_usage(usage)
    inp, out = counts['prompt_tokens'], counts['completion_tokens']
    if inp is None or out is None:
        return None, None, None
    rates = dict(prices)
    tiers = prices.get('tiers') or []
    if not isinstance(tiers, list) or any(
        not isinstance(tier, dict) or not isinstance(tier.get('tier'), dict)
        or _amount(tier['tier'].get('size')) is None for tier in tiers
    ):
        return None, None, None
    for tier in sorted(tiers, key=lambda t: float(t['tier']['size'])):
        if inp > float(tier['tier']['size']):
            for source, target in (('input', 'input_price_per_m'), ('output', 'output_price_per_m'), ('cache_read', 'cache_read_price_per_m'), ('cache_write', 'cache_write_price_per_m')):
                if source in tier:
                    rates[target] = tier[source]
    read, write = counts['cache_read_tokens'], counts['cache_write_tokens']
    if read + write > inp:
        return None, None, None
    amounts = ((inp - read - write, 'input_price_per_m'), (out, 'output_price_per_m'), (read, 'cache_read_price_per_m'), (write, 'cache_write_price_per_m'))
    # Both base rates must be known, including explicit zero for input-only APIs.
    if _amount(rates.get('input_price_per_m')) is None or _amount(rates.get('output_price_per_m')) is None:
        return None, None, None
    if any(tokens and _amount(rates.get(key)) is None for tokens, key in amounts):
        return None, None, None
    cost = sum(tokens * (_amount(rates.get(key)) or 0) / 1_000_000 for tokens, key in amounts)
    return cost, str(rates.get('currency') or 'USD'), 'model_pricing'


def initialize(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS durable_usage (
        run_id TEXT PRIMARY KEY, config_id TEXT, symbol TEXT, provider_id TEXT,
        purpose TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
        cost REAL, currency TEXT, cost_source TEXT, prices_json TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_durable_usage_time ON durable_usage(started_at)')
    conn.execute('CREATE TABLE IF NOT EXISTS durable_usage_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    if conn.execute("SELECT 1 FROM durable_usage_meta WHERE key='legacy_imported'").fetchone():
        return
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'token_usage' in tables:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(token_usage)')}
        if {'model', 'timestamp', 'prompt_tokens', 'completion_tokens', 'config_id', 'symbol'}.issubset(columns):
            legacy_prices = {}
            if 'model_pricing' in tables:
                for row in conn.execute('SELECT * FROM model_pricing'):
                    legacy_prices[row['model']] = dict(row)
            for row in conn.execute('SELECT rowid AS legacy_id,* FROM token_usage').fetchall():
                usage = {'prompt_tokens': row['prompt_tokens'], 'completion_tokens': row['completion_tokens']}
                counts = normalize_usage(usage)
                prices = legacy_prices.get(row['model'], {})
                cost, currency, _ = calculate_cost(usage, prices)
                conn.execute('''INSERT OR IGNORE INTO durable_usage(run_id,config_id,symbol,purpose,model,status,started_at,finished_at,prompt_tokens,completion_tokens,total_tokens,cost,currency,cost_source,prices_json)
                    VALUES (?,?,?,'legacy',?,'success',?,?,?,?,?,?,?, ?,?)''',
                    (f"legacy:{row['legacy_id']}", row['config_id'], row['symbol'], row['model'] or 'unknown', row['timestamp'] or '', row['timestamp'], counts['prompt_tokens'], counts['completion_tokens'], counts['total_tokens'], cost, currency, 'legacy_estimate' if cost is not None else None, json.dumps(prices)))
    conn.execute("INSERT OR IGNORE INTO durable_usage_meta VALUES ('legacy_imported',?)", (datetime.now(timezone.utc).isoformat(),))


def start_usage(conn, run_id: str, *, config_id: str, purpose: str, model: str, provider_id: str | None, started_at: str, prices: dict, symbol: str = '') -> None:
    initialize(conn)
    conn.execute('''INSERT OR IGNORE INTO durable_usage(run_id,config_id,symbol,provider_id,purpose,model,status,started_at,prices_json)
        VALUES (?,?,?,?,?,?,'running',?,?)''', (run_id, config_id, symbol, provider_id, purpose, model, started_at, json.dumps(prices)))


def finish_usage(conn, run_id: str, usage: dict, status: str, finished_at: str) -> dict | None:
    initialize(conn)
    row = conn.execute('SELECT * FROM durable_usage WHERE run_id=?', (run_id,)).fetchone()
    if not row:
        return None
    if row['status'] != 'running':
        return dict(row)
    counts = normalize_usage(usage)
    cost, currency, source = calculate_cost(usage, json.loads(row['prices_json']))
    conn.execute('''UPDATE durable_usage SET status=?,finished_at=?,prompt_tokens=?,completion_tokens=?,total_tokens=?,
        cache_read_tokens=?,cache_write_tokens=?,reasoning_tokens=?,cost=?,currency=?,cost_source=? WHERE run_id=? AND status='running' ''',
        (status, finished_at, counts['prompt_tokens'], counts['completion_tokens'], counts['total_tokens'], counts['cache_read_tokens'], counts['cache_write_tokens'], counts['reasoning_tokens'], cost, currency, source, run_id))
    return dict(conn.execute('SELECT * FROM durable_usage WHERE run_id=?', (run_id,)).fetchone())


def token_stats() -> dict:
    from backend.database import get_db_conn, get_all_pricing
    from backend.config import config
    with get_db_conn() as conn:
        initialize(conn)
        conn.commit()
        rows = [dict(r) for r in conn.execute('SELECT * FROM durable_usage')]
    timezone_name = getattr(config, 'timezone', 'Asia/Shanghai')
    tz = ZoneInfo(timezone_name)
    today = datetime.now(tz).date().isoformat()
    def day_of(value):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)).date().isoformat()
        except (ValueError, TypeError):
            return str(value or '')[:10]
    def aggregate(group):
        costs = defaultdict(float)
        for row in group:
            if row['cost'] is not None:
                costs[row['currency'] or 'USD'] += row['cost']
        return {'prompt': sum(r['prompt_tokens'] or 0 for r in group), 'completion': sum(r['completion_tokens'] or 0 for r in group), 'total': sum(r['total_tokens'] or 0 for r in group), 'calls': len(group), 'failed_calls': sum(r['status'] == 'error' for r in group), 'unpriced_calls': sum(r['cost'] is None and r['status'] != 'running' for r in group), 'costs_by_currency': dict(costs), 'cost': next(iter(costs.values())) if len(costs) == 1 else None, 'currency': next(iter(costs)) if len(costs) == 1 else None, 'cache_read_tokens': sum(r['cache_read_tokens'] or 0 for r in group), 'cache_write_tokens': sum(r['cache_write_tokens'] or 0 for r in group), 'reasoning_tokens': sum(r['reasoning_tokens'] or 0 for r in group)}
    def grouped(field):
        buckets = defaultdict(list)
        for row in rows:
            buckets[row.get(field) or 'unknown'].append(row)
        return [{field: key, **aggregate(group)} for key, group in sorted(buckets.items())]
    daily = defaultdict(list)
    for row in rows:
        daily[day_of(row['started_at'])].append(row)
    return {'daily': [{'day': day, **aggregate(daily[day])} for day in sorted(daily, reverse=True)[:14]],
            'models': grouped('model'), 'agents': grouped('config_id'), 'providers': grouped('provider_id'), 'purposes': grouped('purpose'),
            'today': aggregate(daily.get(today, [])), 'summary': aggregate(rows), 'pricing': get_all_pricing()}
