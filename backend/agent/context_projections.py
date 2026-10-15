"""Published memory and summary text, with program-owned source provenance."""
from __future__ import annotations
from backend.storage import table_columns, record as storage_record

import hashlib
import json
import time
import logging


from backend import database
from backend.utils import trade_evidence
from backend.utils.trade_recovery import operation_evidence

VERSION = 2
MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)
SOURCE_TABLES = {'short_memory': 'short_memories', 'strategy_summary': 'summaries'}


def enabled(cfg: dict | None) -> bool:
    return bool(cfg) and str(cfg.get('mode', '')).upper() in {'REAL', 'STRATEGY'}


def initialize_schema(conn):
    """Schema is installed by the explicit PostgreSQL migration command."""
    return None


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _source(kind: str, row: dict) -> dict:
    row = storage_record(row)
    text = row.get('market_summary') if kind == 'short_memory' else row.get('strategy_logic')
    ids = row.get('source_summary_ids') or []
    ids = json.loads(ids) if isinstance(ids, str) else ids
    return dict(source_kind=kind, source_id=str(row['id']), text=str(text or ''),
                source_hash=_hash([text, row.get('decision_json'), ids]),
                timestamp=row.get('window_end') or row.get('bucket_end') or row.get('timestamp'), row=row)


def _summary_scope_matches(conn, config_id: str, row: dict, scope: str) -> bool:
    source = _source('strategy_summary', row)
    scopes = {p['account_scope'] for p in conn.execute(
        'SELECT account_scope FROM decision_context_projections WHERE config_id=%s '
        "AND source_kind='strategy_summary' AND source_id=%s AND source_hash=%s",
        (config_id, source['source_id'], source['source_hash']))}
    return not scopes or scope in scopes


def current_sources(config_id: str, kind: str | None = None) -> list[dict]:
    from backend.config import config
    from backend.summary_memory_source import valid_compressed_summary
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
    predicate, args = '', [config_id]
    if symbol:
        symbol = str(symbol).upper()
        canonical = symbol if ':' in symbol else symbol + ':' + symbol.split('/')[-1]
        predicate = " AND UPPER(CASE WHEN strpos(symbol,':')>0 THEN symbol ELSE symbol || ':' || substr(symbol,strpos(symbol,'/')+1) END)=%s"
        args.append(canonical)
    with database.get_db_conn() as conn:
        memories = conn.execute('SELECT * FROM short_memories WHERE config_id=%s' + predicate +
                                ' ORDER BY bucket_end DESC NULLS LAST,id DESC LIMIT 1', args).fetchall() if kind != 'strategy_summary' else []
        summaries = []
        if kind != 'short_memory':
            scope = trade_evidence.configured_account_scope(config_id)
            for row in conn.execute('SELECT * FROM summaries WHERE config_id=%s' + predicate +
                                    ' ORDER BY timestamp DESC NULLS LAST,id DESC', args):
                if (valid_compressed_summary(row['strategy_logic'])
                        and _summary_scope_matches(conn, config_id, dict(row), scope)):
                    summaries.append(row)
                if len(summaries) == 3:
                    break
    return [_source(kind, dict(r)) for kind, rows in [('short_memory', memories), ('strategy_summary', summaries)] for r in rows]


def memory_summary_rows(config_id: str, rows: list[dict]) -> list[dict]:
    """Retain the task's compressed rows without crossing known source scopes."""
    from backend.config import config
    from backend.summary_memory_source import valid_compressed_summary
    from backend.utils.trade_operations import _same_symbol
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
    scope = trade_evidence.configured_account_scope(config_id)
    result = []
    with database.get_db_conn() as conn:
        for row in rows:
            if not _same_symbol(row.get('symbol'), symbol) or not valid_compressed_summary(row.get('strategy_logic')):
                continue
            if not _summary_scope_matches(conn, config_id, row, scope):
                continue
            result.append(row)
    return result


def source_refs(config_id: str, source: dict) -> list[str]:
    """Candidates derive from the persisted tool chain, never from prose guesses."""
    rows = [source['row']]
    if source['source_kind'] == 'short_memory':
        ids = source['row'].get('source_summary_ids') or []
        ids = json.loads(ids) if isinstance(ids, str) else ids
        with database.get_db_conn() as conn:
            rows = [dict(r) for identity in ids for r in conn.execute(
                'SELECT * FROM summaries WHERE config_id=%s AND id=%s', (config_id, identity)).fetchall()]
    refs = set()
    for row in rows:
        from backend.utils.trade_operations import _decode_receipt
        record = _decode_receipt(row.get('decision_json') or row.get('report_json'))
        if not isinstance(record, dict):
            continue
        for item in record.get('execution_results', []):
            if not isinstance(item, dict):
                continue
            receipt = _decode_receipt(item.get('result'))
            from backend.utils.decision_record import execution_identity
            identity = execution_identity(item)
            if identity:
                refs.add(str(identity))
            if isinstance(receipt, dict):
                refs.update(str(r['operation_id']) for r in [receipt, *[i.get('result', {}) for i in receipt.get('results', [])]]
                            if isinstance(r, dict) and r.get('operation_id'))
    # Older memory can retain references beyond its latest four-hour window.
    with database.get_db_conn() as conn:
        for row in conn.execute('SELECT execution_refs FROM decision_context_projections WHERE config_id=%s '
                                'AND account_scope=%s AND source_kind=%s AND source_id=%s AND source_hash=%s AND projection_version=%s',
                                key(config_id, trade_evidence.configured_account_scope(config_id), source)):
            try:
                saved_refs = json.loads(row['execution_refs'])
                if not isinstance(saved_refs, list) or any(not isinstance(ref, str) for ref in saved_refs):
                    raise ValueError('Invalid saved execution references')
                refs.update(saved_refs)
            except (ValueError, TypeError):
                # Keep identities recovered from the immutable decision chain.
                # A corrupt optional projection cannot invent a global blocker.
                logger.warning('Invalid saved execution refs for %s/%s', config_id, source['source_id'])
    return sorted(refs)


def key(config_id, scope, source):
    return (config_id, scope, source['source_kind'], source['source_id'], source['source_hash'], VERSION)


KEY_WHERE = 'config_id=%s AND account_scope=%s AND source_kind=%s AND source_id=%s AND source_hash=%s AND projection_version=%s'


def _original(conn, config_id: str, projection: dict) -> dict | None:
    from backend.config import config
    from backend.utils.trade_operations import _same_symbol
    table = SOURCE_TABLES[projection['source_kind']]
    row = conn.execute(f'SELECT * FROM {table} WHERE config_id=%s AND id=%s',
                       (config_id, projection['source_id'])).fetchone()
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
    if not row or not symbol or not _same_symbol(row['symbol'], symbol):
        return None
    if projection.get('symbol') and not _same_symbol(projection['symbol'], symbol):
        return None
    source = _source(projection['source_kind'], dict(row))
    return source if source['source_hash'] == projection['source_hash'] else None


def stored_sources(config_id: str, scope: str, kind: str, *, status: str | None = None,
                   diagnostics: list | None = None, limit: int | None = None) -> list[dict]:
    """Pure read of still-valid versions; one corrupt row cannot hide its peers."""
    sources = []
    with database.get_db_conn() as conn:
        conn.execute('BEGIN')
        ordering = 's.timestamp'
        if kind == 'short_memory':
            columns = {column['name'] for column in table_columns(conn, 'short_memories')}
            ordering = "COALESCE(NULLIF(s.window_end,''),s.bucket_end)" if 'window_end' in columns else 's.bucket_end'
        rows = conn.execute('SELECT p.* FROM decision_context_projections p '
                            f'JOIN {SOURCE_TABLES[kind]} s ON CAST(s.id AS TEXT)=p.source_id AND s.config_id=p.config_id '
                            'WHERE p.config_id=%s AND p.account_scope=%s AND p.source_kind=%s AND p.projection_version=%s'
                            + (' AND p.status=%s' if status else '') + f' ORDER BY {ordering} DESC NULLS LAST,s.id DESC',
                            (config_id, scope, kind, VERSION, *((status,) if status else ())))
        for row in rows:
            try:
                projection = storage_record(row)
                source = _original(conn, config_id, projection)
                if source:
                    if not isinstance(projection['strategy_text'], str):
                        raise ValueError('Strategy text is not readable')
                    sources.append({**source, 'projection': projection})
                    if limit and len(sources) >= limit:
                        break
            except (ValueError, TypeError, KeyError):
                logger.exception('Invalid strategy source %s/%s', config_id, row['source_id'])
                if diagnostics is not None:
                    diagnostics.append(f"{kind}:{row['source_id']}")
    return sources


def capture_sources(config_id: str, summary_rows=None) -> dict:
    """Program-owned provenance; never passed to the summarization model."""
    from backend.config import config
    sources = current_sources(config_id)
    latest_memory = next((s for s in sources if s['source_kind'] == 'short_memory'), None)
    scope = trade_evidence.configured_account_scope(config_id)
    previous = stored_sources(config_id, scope, 'short_memory', status='ready', limit=1)
    if previous and not any(s['source_kind'] == 'short_memory' and s['source_id'] == previous[0]['source_id']
                            for s in sources):
        sources.append(previous[0])
    if summary_rows is not None:
        sources = [s for s in sources if s['source_kind'] == 'short_memory']
        sources.extend(_source('strategy_summary', dict(row)) for row in summary_rows)
    return dict(config_id=config_id, account_scope=scope,
                symbol=(config.get_config_by_id(config_id) or {}).get('symbol', ''),
                output_format='plain_text', projection_version=VERSION,
                latest_memory_id=latest_memory['source_id'] if latest_memory else None,
                source_guards=[{k: s[k] for k in ('source_kind', 'source_id', 'source_hash')} for s in sources],
                execution_refs=sorted({ref for s in sources
                                       if s['source_kind'] != 'short_memory'
                                       or any(p['source_id'] == s['source_id'] for p in previous)
                                       for ref in source_refs(config_id, s)}))


def verify_publication_sources(conn, config_id: str, metadata: dict) -> None:
    """Check before the raw-memory upsert, inside its transaction."""
    _publication_scope(config_id, metadata)
    if 'latest_memory_id' in metadata:
        from backend.utils.trade_operations import _same_symbol
        latest = next((row for row in conn.execute('SELECT id,symbol FROM short_memories WHERE config_id=%s '
                                                   'ORDER BY bucket_end DESC NULLS LAST,id DESC', (config_id,))
                       if _same_symbol(row['symbol'], metadata.get('symbol'))), None)
        if (str(latest['id']) if latest else None) != metadata['latest_memory_id']:
            raise ValueError('Newer memory was published during consolidation')
    for source in metadata.get('source_guards', []):
        if not _original(conn, config_id, source):
            raise ValueError('Memory source changed before publication')


def _publication_scope(config_id: str, metadata: dict) -> str:
    from backend.config import config
    from backend.utils.trade_operations import _same_symbol
    scope = trade_evidence.configured_account_scope(config_id)
    if metadata.get('account_scope') != scope:
        raise ValueError('Account changed during memory projection')
    current_symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
    if metadata.get('symbol') and not _same_symbol(metadata['symbol'], current_symbol):
        raise ValueError('Symbol changed during memory projection')
    return scope


def publish_text(conn, config_id: str, kind: str, row: dict, refs: list[str]):
    """Publish source text atomically; legacy storage is retained for audit compatibility."""
    scope = trade_evidence.configured_account_scope(config_id)
    source = _source(kind, row)
    initialize_schema(conn)
    conn.execute("INSERT INTO decision_context_projections "
                 "(config_id,account_scope,source_kind,source_id,source_hash,projection_version,symbol,updated_at,"
                 "strategy_text,execution_refs,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready') "
                 "ON CONFLICT(config_id,account_scope,source_kind,source_id,source_hash,projection_version) DO UPDATE SET "
                 "strategy_text=excluded.strategy_text,execution_refs=excluded.execution_refs,"
                 "status='ready',error='',updated_at=excluded.updated_at",
                 (*key(config_id, scope, source), row['symbol'], time.time(), source['text'], json.dumps(refs)))


def publish_new_memory(conn, config_id: str, row: dict, metadata: dict):
    _publication_scope(config_id, metadata)
    publish_text(conn, config_id, 'short_memory', row, metadata.get('execution_refs', []))


def load_strategy_views(config_id: str) -> tuple[str, str, dict]:
    from backend.config import config
    scope = trade_evidence.configured_account_scope(config_id)
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol', '')
    texts = {}
    refs = set()
    reference_failure = False
    for kind, limit, label in [('short_memory', 1, '短期策略记忆'), ('strategy_summary', 3, '策略摘要')]:
        published = []
        try:
            published = stored_sources(config_id, scope, kind, status='ready', limit=limit)
            texts[kind] = '\n\n'.join(f"[截至 {s['timestamp']}] {s['projection']['strategy_text'] or '(无策略背景)'}"
                                       for s in reversed(published)) or f'（尚无已发布{label}）'
        except Exception:
            logger.exception('Unable to read published %s for %s', kind, config_id)
            texts[kind] = f'（已发布{label}读取失败）'
            reference_failure = True
        # Resolve references separately, including candidates not yet published.
        try:
            candidates = current_sources(config_id, kind)
        except Exception:
            logger.exception('Unable to read current %s references for %s', kind, config_id)
            candidates = []
            reference_failure = True
        for source in [*published, *candidates]:
            try:
                refs.update(source_refs(config_id, source))
            except Exception:
                logger.exception('Unable to read strategy references for %s/%s', config_id, source['source_id'])
                reference_failure = True
    try:
        evidence = operation_evidence(config_id, symbol, sorted(refs), account_scope=scope)
    except Exception:
        logger.exception('Unable to resolve strategy execution references for %s', config_id)
        evidence = {ref: {'resolution': 'unavailable'} for ref in refs}
        reference_failure = True
    return texts['short_memory'], texts['strategy_summary'], evidence


def memory_view_rows(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        text, status = '', 'pending'
        try:
            source = _source('short_memory', row)
            with database.get_db_conn() as conn:
                projection = conn.execute('SELECT * FROM decision_context_projections WHERE ' + KEY_WHERE,
                                          key(row['config_id'], trade_evidence.configured_account_scope(row['config_id']), source)).fetchone()
                if projection:
                    status = projection['status']
                    if status == 'ready' and _original(conn, row['config_id'], dict(projection)):
                        text = projection['strategy_text']
        except Exception:
            status = 'unavailable'
        result.append({**row, 'market_summary': text, 'position_summary': '',
                       'strategy_view_status': status, 'projection_version': VERSION})
    return result
