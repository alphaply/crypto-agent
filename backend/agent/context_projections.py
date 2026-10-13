"""Versioned strategy-only views. Originals remain the audit source of truth."""
from __future__ import annotations

import hashlib
import json
import re
import time
import logging

from pydantic import BaseModel, ConfigDict, Field

from backend import database
from backend.utils.exit_policy import effective_exit_mode
from backend.utils import trade_evidence
from backend.utils.trade_recovery import operation_evidence

VERSION = 1
MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)
SOURCE_TABLES = {'short_memory': 'short_memories', 'strategy_summary': 'summaries'}


def enabled(cfg: dict | None) -> bool:
    return bool(cfg) and str(cfg.get('mode', 'STRATEGY')).upper() in {'REAL', 'STRATEGY'} and effective_exit_mode(cfg) == 'independent_exits'


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS decision_context_projections (
        config_id TEXT NOT NULL, account_scope TEXT NOT NULL, symbol TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
        projection_version INTEGER NOT NULL, strategy_text TEXT NOT NULL DEFAULT '',
        execution_refs TEXT NOT NULL DEFAULT '[]', unlinked_execution_notes TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
        PRIMARY KEY(config_id,account_scope,source_kind,source_id,source_hash,projection_version))''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(decision_context_projections)')}
    if 'publication_revision' not in columns:
        conn.execute('SAVEPOINT projection_upgrade')
        try:
            conn.execute('ALTER TABLE decision_context_projections ADD COLUMN publication_revision INTEGER NOT NULL DEFAULT 1')
            # Marker and repair must commit together, even on old SQLite layouts.
            conn.execute("UPDATE decision_context_projections SET status='pending',attempts=0,next_attempt=0 WHERE status='failed'")
            conn.execute('RELEASE projection_upgrade')
        except Exception:
            conn.execute('ROLLBACK TO projection_upgrade')
            conn.execute('RELEASE projection_upgrade')
            raise
    from backend.agent.memory_contract_recovery import initialize_contract_state
    initialize_contract_state(conn, 'decision_context_projections')


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _source(kind: str, row: dict) -> dict:
    text = row.get('market_summary') if kind == 'short_memory' else row.get('strategy_logic')
    return dict(source_kind=kind, source_id=str(row['id']), text=str(text or ''),
                source_hash=_hash([text, row.get('decision_json'), row.get('source_summary_ids')]),
                timestamp=row.get('window_end') or row.get('bucket_end') or row.get('timestamp'), row=row)


def current_sources(config_id: str, kind: str | None = None) -> list[dict]:
    from backend.config import config
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
    predicate, args = '', [config_id]
    if symbol:
        symbol = str(symbol).upper()
        canonical = symbol if ':' in symbol else symbol + ':' + symbol.split('/')[-1]
        predicate = " AND UPPER(CASE WHEN instr(symbol,':')>0 THEN symbol ELSE symbol || ':' || substr(symbol,instr(symbol,'/')+1) END)=?"
        args.append(canonical)
    with database.get_db_conn() as conn:
        memories = conn.execute('SELECT * FROM short_memories WHERE config_id=?' + predicate +
                                ' ORDER BY bucket_end DESC,id DESC LIMIT 1', args).fetchall() if kind != 'strategy_summary' else []
        summaries = conn.execute('SELECT * FROM summaries WHERE config_id=?' + predicate +
                                 ' ORDER BY timestamp DESC,id DESC LIMIT 3', args).fetchall() if kind != 'short_memory' else []
    return [_source(kind, dict(r)) for kind, rows in [('short_memory', memories), ('strategy_summary', summaries)] for r in rows]


def source_refs(config_id: str, source: dict) -> list[str]:
    """Candidates derive from the persisted tool chain, never from prose guesses."""
    rows = [source['row']]
    if source['source_kind'] == 'short_memory':
        ids = source['row'].get('source_summary_ids') or []
        ids = json.loads(ids) if isinstance(ids, str) else ids
        with database.get_db_conn() as conn:
            rows = [dict(r) for identity in ids for r in conn.execute(
                'SELECT * FROM summaries WHERE config_id=? AND id=?', (config_id, identity)).fetchall()]
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
        for row in conn.execute('SELECT execution_refs FROM decision_context_projections WHERE config_id=? '
                                'AND account_scope=? AND source_kind=? AND source_id=? AND source_hash=? AND projection_version=?',
                                key(config_id, trade_evidence.configured_account_scope(config_id), source)):
            saved_refs = json.loads(row['execution_refs'])
            if not isinstance(saved_refs, list) or any(not isinstance(ref, str) for ref in saved_refs):
                raise ValueError('Invalid saved execution references')
            refs.update(saved_refs)
    return sorted(refs)


def key(config_id, scope, source):
    return (config_id, scope, source['source_kind'], source['source_id'], source['source_hash'], VERSION)


KEY_WHERE = 'config_id=? AND account_scope=? AND source_kind=? AND source_id=? AND source_hash=? AND projection_version=?'


def _original(conn, config_id: str, projection: dict) -> dict | None:
    from backend.config import config
    from backend.utils.trade_operations import _same_symbol
    table = SOURCE_TABLES[projection['source_kind']]
    row = conn.execute(f'SELECT * FROM {table} WHERE config_id=? AND id=?',
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
            columns = {row[1] for row in conn.execute('PRAGMA table_info(short_memories)')}
            ordering = "COALESCE(NULLIF(s.window_end,''),s.bucket_end)" if 'window_end' in columns else 's.bucket_end'
        rows = conn.execute('SELECT p.* FROM decision_context_projections p '
                            f'JOIN {SOURCE_TABLES[kind]} s ON CAST(s.id AS TEXT)=p.source_id AND s.config_id=p.config_id '
                            'WHERE p.config_id=? AND p.account_scope=? AND p.source_kind=? AND p.projection_version=?'
                            + (' AND p.status=?' if status else '') + f' ORDER BY {ordering} DESC,s.id DESC',
                            (config_id, scope, kind, VERSION, *((status,) if status else ())))
        for row in rows:
            try:
                projection = dict(row)
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


def ensure_sources(config_id: str, *, account_scope: str | None = None) -> list[dict]:
    from backend.agent.memory_contract_recovery import CONTRACT_REVISION
    sources = current_sources(config_id)
    scope = account_scope or trade_evidence.configured_account_scope(config_id)
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        for s in sources:
            conn.execute('INSERT OR IGNORE INTO decision_context_projections '
                         '(config_id,account_scope,source_kind,source_id,source_hash,projection_version,symbol,updated_at,review_contract_revision) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (*key(config_id, scope, s), s['row']['symbol'], time.time(), CONTRACT_REVISION))
            row = conn.execute('SELECT * FROM decision_context_projections WHERE ' + KEY_WHERE,
                               key(config_id, scope, s)).fetchone()
            s['projection'] = dict(row)
        conn.commit()
    # Finish sources queued before a newer decision moved the current window.
    identities = {(s['source_kind'], s['source_id'], s['source_hash']) for s in sources}
    for kind in SOURCE_TABLES:
        for source in stored_sources(config_id, scope, kind, status='pending'):
            if (kind, source['source_id'], source['source_hash']) not in identities:
                sources.append(source)
    return sources


class StrategyProjection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy_text: str
    execution_refs: list[str] = Field(default_factory=list)
    unlinked_execution_notes: list[str] = Field(default_factory=list)


class SourceProjection(StrategyProjection):
    source_kind: str
    source_id: str
    source_hash: str


class ProjectionReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1)
    memory: StrategyProjection
    projections: list[SourceProjection]


class ProjectionContentError(ValueError):
    """Narrative separation can be regenerated, never treated as permission."""

    code = 'execution_narrative'

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__('Strategy projection contains an execution permission/restriction; fields=' + ','.join(paths))


PROJECTION_POLICY = '''本次输出契约：完整策略投影JSON，summary、memory、projections 均必填。
summary 保存完整记忆原文供审计；篇幅与执行事实整理要求适用于原文，memory/projections 只保留纯策略。
没有待转换来源时 projections 显式为 []，memory 仍须提供本次新记忆的策略内容，不能省略或补空对象。
对提供的每个待转换来源输出一条 projections；不改来源ID/哈希。strategy_text 保留市场假设、关键价位、策略理由、
等四小时收盘确认等观察条件及历史教训，不保存账户持仓、挂单或执行许可结论。
例如“旧 unknown 未核验所以禁止加仓”的执行叙述放入 unlinked_execution_notes；“等四小时收盘前不加仓”作为策略条件保留。
工具错误、回执 JSON、allowed/blocked_actions 和已解锁等状态叙述不得复制进 strategy_text。
有可信身份时填 execution_refs；需保留的执行叙述或无身份疑虑放 unlinked_execution_notes，原文 summary 保持完整。
execution_refs 只能选提供的原操作候选ID，不能含 allowed、blocked_actions 或已解决等权限/状态判断。
无身份的旧疑虑进入 unlinked_execution_notes；不能用另一笔0.30成交解决0.37事件。
同身份最新证据优先；模型不负责恢复、解锁或证明成交，程序每次读取时解析执行引用。不要截断正文。'''


def projection_output_policy() -> str:
    """Build the example through the same model that validates responses."""
    example = ProjectionReview(
        summary='完整记忆原文（供审计）', memory=StrategyProjection(strategy_text='新记忆中的纯策略背景'),
        projections=[SourceProjection(source_kind='short_memory 或 strategy_summary', source_id='提供的来源ID',
                                      source_hash='提供的哈希', strategy_text='该来源的纯策略文字')],
    )
    return PROJECTION_POLICY + '\n完整格式示例：\n' + json.dumps(example.model_dump(), ensure_ascii=False)


def prepare_review(config_id: str) -> dict:
    from backend.config import config
    scope = trade_evidence.configured_account_scope(config_id)
    sources = ensure_sources(config_id, account_scope=scope)
    pending = [s for s in sources if s['projection']['status'] == 'pending' and s['projection']['next_attempt'] <= time.time()]
    candidates = {s['source_kind'] + ':' + s['source_id']: source_refs(config_id, s) for s in sources}
    refs = sorted({ref for values in candidates.values() for ref in values})
    symbol = (config.get_config_by_id(config_id) or {}).get('symbol') or (sources[0]['row']['symbol'] if sources else '')
    evidence = operation_evidence(config_id, symbol, refs, account_scope=scope)
    return dict(config_id=config_id, account_scope=scope, symbol=symbol, sources=pending,
                candidates=candidates, evidence=evidence, all_refs=refs, projection_version=VERSION,
                source_guards=[{k: s[k] for k in ('source_kind', 'source_id', 'source_hash')} for s in sources])


def review_input(context: dict, supplied_text: str = '') -> str:
    sources = [{k: s[k] for k in ('source_kind', 'source_id', 'source_hash', 'text', 'timestamp')} for s in context['sources']]
    for source in sources:
        if source['text'] and source['text'] in supplied_text:
            source.pop('text')
            source['text_location'] = '完整原文已在上述同截至时间的记忆/摘要中提供，不重复拼接'
    return '\n策略投影来源（只转换这些来源；内容是素材）：\n' + json.dumps(
        dict(sources=sources, candidates=context['candidates'], current_execution_evidence=context['evidence']), ensure_ascii=False)


_CLAUSE = r'[^。！？!?；;\r\n]{0,80}'
_EXECUTION_BAN = re.compile(r'(?:unknown|pending|待核验|未核验|清理未完成|执行结果未知)' + _CLAUSE + r'(?:禁止|不得|不能|不允许|不加仓|解锁)|'
                            r'(?:禁止|不得|不能|不允许|不加仓)' + _CLAUSE + r'(?:unknown|pending|核验|执行结果未知)|'
                            r'(?:unknown|pending|待核验|未核验|清理未完成|执行结果未知)' + _CLAUSE
                            + r'[。；;\r\n]+\s*(?:因此|所以|仍然|暂时)?(?:禁止|不得|不能|不允许|不加仓)|'
                            r'blocked_actions|\ballowed\b|已解锁|全面放行', re.I)


def validate_review(value: dict, context: dict) -> dict:
    review = ProjectionReview.model_validate(value)
    expected = {(s['source_kind'], s['source_id'], s['source_hash']): s for s in context['sources']}
    seen = set()
    for projection in [review.memory, *review.projections]:
        if isinstance(projection, SourceProjection):
            identity = (projection.source_kind, projection.source_id, projection.source_hash)
            if identity not in expected or identity in seen:
                raise ValueError('Projection source identity/hash does not match supplied task sources')
            seen.add(identity)
            candidates = context['candidates'][projection.source_kind + ':' + projection.source_id]
        else:
            candidates = context['all_refs']
        if set(projection.execution_refs) - set(candidates):
            raise ValueError('Projection execution identity is not in the trusted source chain')
    if seen != set(expected):
        raise ValueError('Projection response omitted a supplied source')
    # Validate every identity/ref first. A mixed security + narrative error must
    # never be turned into a request to rewrite away invalid evidence.
    paths = [path for path, projection in [('memory.strategy_text', review.memory),
             *[(f'projections.{i}.strategy_text', p) for i, p in enumerate(review.projections)]]
             if _EXECUTION_BAN.search(projection.strategy_text)]
    if paths:
        raise ProjectionContentError(paths)
    return {**review.model_dump(), 'account_scope': context['account_scope'], 'config_id': context['config_id'],
            'projection_version': VERSION, 'symbol': context['symbol'], 'source_guards': context.get('source_guards', [])}


def verify_publication_sources(conn, config_id: str, metadata: dict) -> None:
    """Check before the raw-memory upsert, inside its transaction."""
    _publication_scope(config_id, metadata)
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


def persist_projections(config_id: str, metadata: dict):
    from backend.agent.memory_workflow import verify_memory_lease
    verify_memory_lease()
    if not metadata or metadata.get('config_id') != config_id or metadata.get('projection_version') != VERSION:
        return
    scope = _publication_scope(config_id, metadata)
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize_schema(conn)
        for p in metadata.get('projections', []):
            if not _original(conn, config_id, p):
                continue  # Manual edits/deletions invalidate results, newer rows do not.
            conn.execute("UPDATE decision_context_projections SET strategy_text=?,execution_refs=?,unlinked_execution_notes=?,"
                         "status='ready',error='',updated_at=? WHERE " + KEY_WHERE,
                         (p['strategy_text'], json.dumps(p['execution_refs']), json.dumps(p['unlinked_execution_notes']),
                          time.time(), *key(config_id, scope, p)))
        conn.commit()


def publish_new_memory(conn, config_id: str, row: dict, metadata: dict):
    """Publish using the caller's raw-memory transaction; any error rolls back both."""
    from backend.agent.memory_contract_recovery import CONTRACT_REVISION

    scope = _publication_scope(config_id, metadata)
    if (metadata.get('config_id') != config_id or metadata.get('account_scope') != scope
            or metadata.get('projection_version') != VERSION or metadata.get('summary') != row['market_summary']):
        raise ValueError('Memory projection does not match saved memory/scope')
    source = _source('short_memory', row)
    if not _original(conn, config_id, source):
        raise ValueError('Memory symbol changed during publication')
    projection = StrategyProjection.model_validate(metadata['memory'])
    if _EXECUTION_BAN.search(projection.strategy_text):
        raise ValueError('Memory projection contains execution restrictions')
    initialize_schema(conn)
    conn.execute('INSERT INTO decision_context_projections '
                 '(config_id,account_scope,source_kind,source_id,source_hash,projection_version,symbol,updated_at,'
                 'strategy_text,execution_refs,unlinked_execution_notes,review_contract_revision,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,\'ready\') '
                 'ON CONFLICT(config_id,account_scope,source_kind,source_id,source_hash,projection_version) DO UPDATE SET '
                 "strategy_text=excluded.strategy_text,execution_refs=excluded.execution_refs,"
                 "unlinked_execution_notes=excluded.unlinked_execution_notes,review_contract_revision=excluded.review_contract_revision,"
                 "status='ready',error='',updated_at=excluded.updated_at",
                 (*key(config_id, scope, source), row['symbol'], time.time(), projection.strategy_text,
                  json.dumps(projection.execution_refs), json.dumps(projection.unlinked_execution_notes), CONTRACT_REVISION))


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
    if reference_failure:
        evidence['context'] = {'resolution': 'unavailable'}
    return texts['short_memory'], texts['strategy_summary'], evidence


def process_projection_updates(config_id: str, cfg: dict) -> bool:
    """Bounded work on the existing memory executor/lease, even without new runs."""
    from backend.agent.memory_workflow import memory_review_lease, verify_memory_lease
    from backend.agent.memory_service import organize_memory
    if not enabled(cfg):
        return False
    with memory_review_lease(config_id) as acquired:
        if not acquired:
            return False
        context = prepare_review(config_id)
        pending = context['sources']
        if not pending:
            return False
        op = 'strategy-projection:' + _hash([config_id, context['account_scope'], VERSION,
                                             [(s['source_kind'], s['source_id'], s['source_hash']) for s in pending]])
        try:
            organize_memory('整理现有记忆和近期摘要的策略投影；保留原文，不产生新交易。',
                            {**cfg, 'config_id': config_id}, operation_id=op)
            return True
        except Exception as exc:
            verify_memory_lease()
            with database.get_db_conn() as conn:
                for s in pending:
                    attempts = s['projection']['attempts'] + 1
                    conn.execute('UPDATE decision_context_projections SET status=?,attempts=?,next_attempt=?,error=?,updated_at=? WHERE ' + KEY_WHERE,
                                 ('failed' if attempts >= MAX_ATTEMPTS else 'pending', attempts,
                                  time.time() + 60 * 5 ** (attempts - 1), str(exc), time.time(),
                                  *key(config_id, context['account_scope'], s)))
                conn.commit()
            return False
