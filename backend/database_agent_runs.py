"""Private local traces of actual agent LLM calls, without provider credentials."""

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


PURPOSES = frozenset({'decision', 'strategy_summary', 'memory_review', 'daily_summary', 'chat', 'chat_summary', 'news_score', 'news_summary'})
STATUSES = frozenset({'success', 'error', 'cancelled'})
RETENTION_DAYS = 30
MAX_RUNS_PER_CONFIG = 100
# Twenty maximum-size news batches (300 items, up to two score attempts) plus
# summary retries. Ordinary agent retention remains unchanged.
MAX_NEWS_RUNS = 12100
_SECRET_KEYS = frozenset({
    'api_key', 'apikey', 'authorization', 'password', 'secret', 'passphrase',
    'access_token', 'refresh_token', 'config', 'configurable', 'credentials',
})
_MESSAGE_KEYS = ('role', 'content', 'name', 'tool_call_id', 'tool_calls', 'function_call', 'refusal')
_METADATA_COLUMNS = '''run_id, parent_run_id, config_id, purpose, model, provider_id, status,
    started_at, finished_at, input_chars, message_chars, tool_chars, output_chars,
    prompt_tokens, completion_tokens, total_tokens, cost, currency, cost_source'''


def initialize_agent_runs_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute('''CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY, parent_run_id TEXT, config_id TEXT NOT NULL,
        purpose TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT,
        input_chars INTEGER NOT NULL, message_chars INTEGER NOT NULL,
        tool_chars INTEGER NOT NULL, output_chars INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        cost REAL, currency TEXT, cost_source TEXT,
        input_price_per_m REAL, output_price_per_m REAL, pricing_currency TEXT,
        messages_json TEXT NOT NULL, tools_json TEXT NOT NULL,
        output TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}'
    )''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_agent_runs_config_time
        ON agent_runs(config_id, started_at DESC)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_agent_runs_time
        ON agent_runs(started_at DESC)''')
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(agent_runs)')}
    for name, column_type in (
        ('cost_source', 'TEXT'), ('input_price_per_m', 'REAL'),
        ('output_price_per_m', 'REAL'), ('pricing_currency', 'TEXT'), ('provider_id', 'TEXT'),
    ):
        if name not in existing:
            cursor.execute(f'ALTER TABLE agent_runs ADD COLUMN {name} {column_type}')


def _json_value(value: Any) -> Any:
    """Never serialize arbitrary object reprs or their runtime configuration."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key): '[redacted]' if str(key).lower() in _SECRET_KEYS else _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return '[unsupported value]'


def _serialize_message(message: Any) -> dict:
    if isinstance(message, dict):
        return _json_value({key: message[key] for key in _MESSAGE_KEYS if key in message})
    role = getattr(message, 'role', None) or {
        'human': 'user', 'ai': 'assistant', 'system': 'system',
        'tool': 'tool', 'function': 'function',
    }.get(getattr(message, 'type', ''), getattr(message, 'type', 'unknown'))
    result = {'role': role, 'content': _json_value(getattr(message, 'content', ''))}
    for key in ('name', 'tool_call_id', 'tool_calls'):
        value = getattr(message, key, None)
        if value:
            result[key] = _json_value(value)
    # Some provider adapters keep raw tool calls here rather than on AIMessage.
    extra = getattr(message, 'additional_kwargs', {}) or {}
    for key in ('tool_calls', 'function_call', 'refusal'):
        if key not in result and key in extra:
            result[key] = _json_value(extra[key])
    return result


def _serialize_tools(tools: list | None) -> list:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [_json_value(tool if isinstance(tool, dict) else convert_to_openai_tool(tool)) for tool in (tools or [])]


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
        return amount if math.isfinite(amount) and amount >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pricing_snapshot(conn: sqlite3.Connection, model: str) -> tuple:
    """Read configured rates only; an absent price is not a free model."""
    try:
        row = conn.execute('''SELECT input_price_per_m, output_price_per_m, currency
            FROM model_pricing WHERE model = ?''', (model,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return None, None, None
        raise
    if row is None:
        return None, None, None
    return _amount(row[0]), _amount(row[1]), str(row[2] or 'USD')


def _prune_expired(conn: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec='microseconds')
    conn.execute('DELETE FROM agent_runs WHERE started_at < ?', (cutoff,))


def _prune(conn: sqlite3.Connection, config_id: str) -> None:
    _prune_expired(conn)
    conn.execute('''DELETE FROM agent_runs WHERE config_id = ? AND run_id NOT IN (
        SELECT run_id FROM agent_runs WHERE config_id = ?
        ORDER BY started_at DESC, rowid DESC LIMIT ?
    )''', (config_id, config_id, MAX_NEWS_RUNS if config_id == 'news-intelligence' else MAX_RUNS_PER_CONFIG))


def start_agent_run(
    config_id: str, purpose: str, model: str, messages: list, tools: list | None = None,
    *, parent_run_id: str | None = None, provider_id: str | None = None,
    price_snapshot: dict | None = None,
) -> str:
    """Record the actual inputs immediately before invoking the model."""
    from backend.database import get_db_conn
    from backend.app.services.pricing_service import get_price_snapshot, resolve_provider_id
    from backend.database_usage import start_usage

    if purpose not in PURPOSES:
        raise ValueError('Unknown agent run purpose')
    messages_json = _dumps([_serialize_message(message) for message in messages])
    tools_json = _dumps(_serialize_tools(tools))
    run_id = uuid.uuid4().hex
    provider_id = provider_id or resolve_provider_id(str(model), str(config_id), purpose)
    started_at = _timestamp()
    with get_db_conn() as conn:
        initialize_agent_runs_schema(conn.cursor())
        prices = price_snapshot if price_snapshot is not None else get_price_snapshot(str(model), provider_id, conn=conn)
        input_price, output_price, pricing_currency = prices.get('input_price_per_m'), prices.get('output_price_per_m'), prices.get('currency')
        conn.execute('''INSERT INTO agent_runs (
            run_id, parent_run_id, config_id, purpose, model, status, started_at,
            input_chars, message_chars, tool_chars, messages_json, tools_json,
            input_price_per_m, output_price_per_m, pricing_currency, provider_id
        ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
            run_id, parent_run_id, str(config_id), purpose, str(model), started_at,
            len(messages_json) + len(tools_json), len(messages_json), len(tools_json),
            messages_json, tools_json, input_price, output_price, pricing_currency, provider_id,
        ))
        start_usage(conn, run_id, config_id=str(config_id), purpose=purpose, model=str(model), provider_id=provider_id, started_at=started_at, prices=prices)
        _prune(conn, str(config_id))
        conn.commit()
    return run_id


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 and float(value) == parsed else None
    except (TypeError, ValueError, OverflowError):
        return None


def finish_agent_run(
    run_id: str, *, status: str, output: str = '', usage: dict | None = None,
    error: str = '', details: dict | None = None,
) -> None:
    from backend.database import get_db_conn
    from backend.database_usage import finish_usage

    if status not in STATUSES:
        raise ValueError('Unknown agent run status')
    usage = usage or {}
    prompt_tokens = _count(usage.get('prompt_tokens', usage.get('input_tokens')))
    completion_tokens = _count(usage.get('completion_tokens', usage.get('output_tokens')))
    total_tokens = _count(usage.get('total_tokens'))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    cost = _amount(usage.get('cost'))
    currency = str(usage.get('currency') or 'USD') if cost is not None else None
    cost_source = 'provider' if cost is not None else None
    with get_db_conn() as conn:
        initialize_agent_runs_schema(conn.cursor())
        finished_at = _timestamp()
        durable = finish_usage(conn, run_id, usage, status, finished_at)
        price = conn.execute('''SELECT input_price_per_m, output_price_per_m, pricing_currency
            FROM agent_runs WHERE run_id = ?''', (run_id,)).fetchone()
        if cost is None and prompt_tokens is not None and completion_tokens is not None and price is not None:
            input_price, output_price = _amount(price[0]), _amount(price[1])
            if input_price is not None and output_price is not None:
                # Same input/output-per-million calculation as token statistics,
                # using this call's saved rates rather than today's edited rates.
                cost = prompt_tokens / 1_000_000 * input_price + completion_tokens / 1_000_000 * output_price
                currency = price[2]
                cost_source = 'model_pricing'
        if durable is not None:
            prompt_tokens, completion_tokens, total_tokens = durable['prompt_tokens'], durable['completion_tokens'], durable['total_tokens']
            cost, currency, cost_source = durable['cost'], durable['currency'], durable['cost_source']
        conn.execute('''UPDATE agent_runs SET status = ?, finished_at = ?, output = ?,
            output_chars = ?, prompt_tokens = ?, completion_tokens = ?, total_tokens = ?,
            cost = ?, currency = ?, cost_source = ?, error = ?, details_json = ? WHERE run_id = ?''', (
            status, finished_at, output, len(output), prompt_tokens, completion_tokens,
            total_tokens, cost, currency, cost_source, error, _dumps(_json_value(details or {})), run_id,
        ))
        conn.commit()


def list_agent_runs(*, config_id: str | None = None, purpose: str | None = None,
                    limit: int = 20, offset: int = 0) -> dict:
    from backend.database import get_db_conn

    if limit < 1 or limit > 100 or offset < 0 or offset > 10000:
        raise ValueError('Pagination outside supported range')
    filters, values = [], []
    if config_id is not None:
        filters.append('config_id = ?')
        values.append(config_id)
    if purpose is not None:
        if purpose not in PURPOSES:
            raise ValueError('Unknown agent run purpose')
        filters.append('purpose = ?')
        values.append(purpose)
    where = ' WHERE ' + ' AND '.join(filters) if filters else ''
    with get_db_conn() as conn:
        initialize_agent_runs_schema(conn.cursor())
        _prune_expired(conn)
        conn.commit()
        total = conn.execute('SELECT COUNT(*) FROM agent_runs' + where, values).fetchone()[0]
        rows = conn.execute('SELECT ' + _METADATA_COLUMNS + ' FROM agent_runs' + where +
                            ' ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
    return {'runs': [dict(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset,
            'retention': {'days': RETENTION_DAYS,
                          'per_config': MAX_NEWS_RUNS if config_id == 'news-intelligence' else MAX_RUNS_PER_CONFIG,
                          'news_per_config': MAX_NEWS_RUNS}}


def get_agent_run(run_id: str) -> dict | None:
    from backend.database import get_db_conn

    with get_db_conn() as conn:
        initialize_agent_runs_schema(conn.cursor())
        _prune_expired(conn)
        conn.commit()
        row = conn.execute('SELECT * FROM agent_runs WHERE run_id = ?', (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ('messages', 'tools', 'details'):
        result[key] = json.loads(result.pop(key + '_json'))
    return result
