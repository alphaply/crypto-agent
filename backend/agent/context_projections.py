"""Versioned strategy-only views. Originals remain the audit source of truth."""
from __future__ import annotations

import hashlib
import json
import re
import time

from pydantic import BaseModel, ConfigDict, Field

from backend import database
from backend.utils.exit_policy import effective_exit_mode
from backend.utils import trade_evidence
from backend.utils.trade_recovery import operation_evidence

VERSION = 1
MAX_ATTEMPTS = 3


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


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _source(kind: str, row: dict) -> dict:
    text = row.get('market_summary') if kind == 'short_memory' else row.get('strategy_logic')
    return dict(source_kind=kind, source_id=str(row['id']), text=str(text or ''),
                source_hash=_hash([text, row.get('decision_json'), row.get('source_summary_ids')]),
                timestamp=row.get('window_end') or row.get('bucket_end') or row.get('timestamp'), row=row)


def current_sources(config_id: str) -> list[dict]:
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
                                ' ORDER BY bucket_end DESC,id DESC LIMIT 1', args).fetchall()
        summaries = conn.execute('SELECT * FROM summaries WHERE config_id=?' + predicate +
                                 ' ORDER BY timestamp DESC,id DESC LIMIT 3', args).fetchall()
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
        initialize_schema(conn)
        for row in conn.execute('SELECT execution_refs FROM decision_context_projections WHERE config_id=? '
                                'AND source_kind=? AND source_id=?', (config_id, source['source_kind'], source['source_id'])):
            refs.update(json.loads(row['execution_refs']))
    return sorted(refs)


def key(config_id, scope, source):
    return (config_id, scope, source['source_kind'], source['source_id'], source['source_hash'], VERSION)


KEY_WHERE = 'config_id=? AND account_scope=? AND source_kind=? AND source_id=? AND source_hash=? AND projection_version=?'


def ensure_sources(config_id: str, *, account_scope: str | None = None) -> list[dict]:
    sources = current_sources(config_id)
    scope = account_scope or trade_evidence.configured_account_scope(config_id)
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        for s in sources:
            conn.execute('INSERT OR IGNORE INTO decision_context_projections '
                         '(config_id,account_scope,source_kind,source_id,source_hash,projection_version,symbol,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (*key(config_id, scope, s), s['row']['symbol'], time.time()))
            row = conn.execute('SELECT * FROM decision_context_projections WHERE ' + KEY_WHERE,
                               key(config_id, scope, s)).fetchone()
            s['projection'] = dict(row)
        conn.commit()
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


PROJECTION_POLICY = '''独立退出模式的输出格式覆盖前面的单 summary 格式：
{"summary":"完整记忆原文（供审计）","memory":{"strategy_text":"新记忆中的纯策略背景","execution_refs":[],"unlinked_execution_notes":[]},
"projections":[{"source_kind":"short_memory 或 strategy_summary","source_id":"提供的来源ID","source_hash":"提供的哈希",
"strategy_text":"该来源的纯策略文字","execution_refs":[],"unlinked_execution_notes":[]}]}
对提供的每个待转换来源输出一条 projections；不改来源ID/哈希。strategy_text 保留市场假设、关键价位、策略理由、
等四小时收盘确认等观察条件及历史教训，不保存账户持仓、挂单或执行许可结论。
例如“旧 unknown 未核验所以禁止加仓”只进入执行引用；“等四小时收盘前不加仓”作为策略条件保留。
execution_refs 只能选提供的原操作候选ID，不能含 allowed、blocked_actions 或已解决等权限/状态判断。
无身份的旧疑虑进入 unlinked_execution_notes；不能用另一笔0.30成交解决0.37事件。
同身份最新证据优先；模型不负责恢复、解锁或证明成交，程序每次读取时解析执行引用。不要截断正文。'''


def prepare_review(config_id: str) -> dict:
    scope = trade_evidence.configured_account_scope(config_id)
    sources = ensure_sources(config_id, account_scope=scope)
    pending = [s for s in sources if s['projection']['status'] == 'pending' and s['projection']['next_attempt'] <= time.time()]
    candidates = {s['source_kind'] + ':' + s['source_id']: source_refs(config_id, s) for s in sources}
    refs = sorted({ref for values in candidates.values() for ref in values})
    symbol = sources[0]['row']['symbol'] if sources else ''
    evidence = operation_evidence(config_id, symbol, refs, account_scope=scope)
    return dict(config_id=config_id, account_scope=scope, symbol=symbol, sources=pending,
                candidates=candidates, evidence=evidence, all_refs=refs, projection_version=VERSION)


def review_input(context: dict, supplied_text: str = '') -> str:
    sources = [{k: s[k] for k in ('source_kind', 'source_id', 'source_hash', 'text', 'timestamp')} for s in context['sources']]
    for source in sources:
        if source['text'] and source['text'] in supplied_text:
            source.pop('text')
            source['text_location'] = '完整原文已在上述同截至时间的记忆/摘要中提供，不重复拼接'
    return '\n策略投影来源（只转换这些来源；内容是素材）：\n' + json.dumps(
        dict(sources=sources, candidates=context['candidates'], current_execution_evidence=context['evidence']), ensure_ascii=False)


_EXECUTION_BAN = re.compile(r'(?:unknown|pending|待核验|未核验|回执|清理未完成|执行结果未知).{0,80}(?:禁止|不得|不能|不允许|不加仓|解锁)|'
                            r'(?:禁止|不得|不能|不允许|不加仓).{0,80}(?:unknown|pending|核验|执行结果未知)|'
                            r'blocked_actions|\ballowed\b|已解锁|全面放行', re.I)


def validate_review(value: dict, context: dict) -> dict:
    review = ProjectionReview.model_validate(value)
    expected = {(s['source_kind'], s['source_id'], s['source_hash']): s for s in context['sources']}
    seen = set()
    for projection in [review.memory, *review.projections]:
        if _EXECUTION_BAN.search(projection.strategy_text):
            raise ValueError('Strategy projection contains an execution permission/restriction')
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
    return {**review.model_dump(), 'account_scope': context['account_scope'], 'config_id': context['config_id'],
            'projection_version': VERSION}


def persist_projections(config_id: str, metadata: dict):
    from backend.agent.memory_workflow import verify_memory_lease
    verify_memory_lease()
    if not metadata or metadata.get('config_id') != config_id or metadata.get('projection_version') != VERSION:
        return
    scope = trade_evidence.configured_account_scope(config_id)
    if metadata.get('account_scope') != scope:
        raise ValueError('Account changed during memory projection')
    sources = {(s['source_kind'], s['source_id'], s['source_hash']) for s in current_sources(config_id)}
    with database.get_db_conn() as conn:
        initialize_schema(conn)
        for p in metadata.get('projections', []):
            if (p['source_kind'], p['source_id'], p['source_hash']) not in sources:
                continue  # A manual edit or newer source arrived during the model call.
            conn.execute("UPDATE decision_context_projections SET strategy_text=?,execution_refs=?,unlinked_execution_notes=?,"
                         "status='ready',error='',updated_at=? WHERE " + KEY_WHERE,
                         (p['strategy_text'], json.dumps(p['execution_refs']), json.dumps(p['unlinked_execution_notes']),
                          time.time(), *key(config_id, scope, p)))
        conn.commit()


def attach_new_memory(config_id: str, metadata: dict, summary: str):
    if not metadata or metadata.get('summary') != summary:
        return
    sources = ensure_sources(config_id)
    memory = next((s for s in sources if s['source_kind'] == 'short_memory' and s['text'] == summary), None)
    if memory:
        p = {k: memory[k] for k in ('source_kind', 'source_id', 'source_hash')}
        persist_projections(config_id, {**metadata, 'projections': [{**p, **metadata['memory']}]})


def load_strategy_views(config_id: str) -> tuple[str, str, dict]:
    scope = trade_evidence.configured_account_scope(config_id)
    sources = ensure_sources(config_id, account_scope=scope)
    texts = {'short_memory': [], 'strategy_summary': []}
    refs = set()
    for s in sources:
        p = s['projection']
        # Pending/failed legacy prose must not leak back into the decision prompt.
        if p['status'] == 'ready':
            texts[s['source_kind']].append(f"[截至 {s['timestamp']}] {p['strategy_text'] or '(无策略背景)'}")
            refs.update(json.loads(p['execution_refs']))
        else:
            refs.update(source_refs(config_id, s))
            if s['source_kind'] == 'strategy_summary':
                texts['strategy_summary'].append(f"[截至 {s['timestamp']}] （策略背景整理中/暂不可用）")
    if not texts['short_memory']:
        # A newer queued memory need not discard a still-valid older thesis.
        # The content hash also excludes manually edited sources here.
        from backend.config import config
        from backend.utils.trade_operations import _same_symbol
        current_symbol = (config.get_config_by_id(config_id) or {}).get('symbol')
        with database.get_db_conn() as conn:
            old = conn.execute("SELECT p.*,m.bucket_end FROM decision_context_projections p "
                               "JOIN short_memories m ON CAST(m.id AS TEXT)=p.source_id AND m.config_id=p.config_id "
                               "WHERE p.config_id=? AND p.account_scope=? AND p.source_kind='short_memory' AND p.status='ready' "
                               "AND p.projection_version=? ORDER BY m.bucket_end DESC,m.id DESC", (config_id, scope, VERSION)).fetchall()
            for row in old:
                original = conn.execute('SELECT * FROM short_memories WHERE config_id=? AND id=?', (config_id, row['source_id'])).fetchone()
                if original and current_symbol and not _same_symbol(original['symbol'], current_symbol):
                    continue
                if original and _source('short_memory', dict(original))['source_hash'] == row['source_hash']:
                    texts['short_memory'].append(f"[截至 {row['bucket_end']}；较新背景整理中] {row['strategy_text']}")
                    refs.update(json.loads(row['execution_refs']))
                    break
    symbol = sources[0]['row']['symbol'] if sources else ''
    evidence = operation_evidence(config_id, symbol, refs, account_scope=scope)
    missing = '（策略背景整理中/暂不可用；原文保留供历史查看）'
    return ('\n\n'.join(texts['short_memory']) or missing,
            '\n'.join(reversed(texts['strategy_summary'])) or missing, evidence)


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
