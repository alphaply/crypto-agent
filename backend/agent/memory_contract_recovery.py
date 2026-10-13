"""One-time, scoped recovery of the old projection output-contract failure."""
from __future__ import annotations

import json
import re

from backend import database

CONTRACT_REVISION = 2
TABLES = {'decision_context_projections', 'memory_update_jobs'}


def initialize_contract_state(conn, table: str) -> None:
    if table not in TABLES:
        raise ValueError('Unsupported memory work table')
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    additions = [('review_contract_revision', 'INTEGER NOT NULL DEFAULT 0'),
                 ('contract_recovery', "TEXT NOT NULL DEFAULT '{}'")]
    if table == 'memory_update_jobs':
        additions.append(('review_source', "TEXT NOT NULL DEFAULT '{}'"))
    missing = [(name, definition) for name, definition in additions if name not in columns]
    if not missing:
        return
    conn.execute('SAVEPOINT memory_contract_upgrade')
    try:
        for name, definition in missing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        conn.execute('RELEASE memory_contract_upgrade')
    except Exception:
        conn.execute('ROLLBACK TO memory_contract_upgrade')
        conn.execute('RELEASE memory_contract_upgrade')
        raise


def is_legacy_projection_failure(error: str) -> bool:
    """Match validator diagnostics, never arbitrary mentions in source prose."""
    error = str(error or '').strip()
    header = re.match(r'([12]) validation errors? for ProjectionReview\r?\n', error)
    if not header:
        return False
    fields = re.findall(r'(?m)^(memory|projections)\r?\n\s+Field required \[type=missing,', error)
    return len(fields) == len(set(fields)) == int(header[1])


def is_recoverable_contract_failure(row: dict) -> bool:
    revision = row['review_contract_revision']
    # Revision 2 must not give revision 1's exhausted format jobs a new budget.
    return (revision < 1 and is_legacy_projection_failure(row['error'])) or (
        revision < 2 and row['error'] == 'Strategy projection contains an execution permission/restriction')


def job_source(conn, config_id: str, summary_id: int, scope: str, symbol: str) -> dict | None:
    from backend.agent.context_projections import _source
    from backend.utils.trade_operations import _same_symbol

    row = conn.execute('SELECT * FROM summaries WHERE config_id=? AND id=?', (config_id, summary_id)).fetchone()
    if not row or not _same_symbol(row['symbol'], symbol):
        return None
    source = _source('strategy_summary', dict(row))
    return {'account_scope': scope, 'symbol': row['symbol'], 'source_id': str(summary_id),
            'source_hash': source['source_hash']}


def _diagnostic(row: dict, **extra) -> str:
    previous = row.get('contract_recovery') or '{}'
    try:
        previous = json.loads(previous)
    except (TypeError, ValueError):
        pass
    if isinstance(previous, dict) and previous.get('revision') == CONTRACT_REVISION:
        previous = previous.get('previous_recovery')
    return json.dumps({'revision': CONTRACT_REVISION, 'previous_error': row['error'],
                       'previous_attempts': row['attempts'],
                       **({'previous_recovery': previous} if previous else {}), **extra}, ensure_ascii=False)


def recover_legacy_failures(config_id: str, cfg: dict) -> dict:
    """Called by background dispatch, not decision reads; never invokes an LLM."""
    from backend.agent import context_projections as projections, memory_updates
    from backend.agent.memory_workflow import memory_review_lease, verify_memory_lease
    from backend.utils.trade_evidence import configured_account_scope

    counts = {'jobs': 0, 'projections': 0}
    if not cfg.get('enabled', True) or not projections.enabled(cfg):
        return counts
    with database.get_db_conn() as conn:
        memory_updates._initialize(conn)
        projections.initialize_schema(conn)
        needed = any(conn.execute(f'SELECT 1 FROM {table} WHERE config_id=? AND status=\'failed\' '
                                  'AND review_contract_revision<? LIMIT 1',
                                  (config_id, CONTRACT_REVISION)).fetchone() for table in TABLES)
    if not needed:
        return counts
    with memory_review_lease(config_id) as acquired:
        if not acquired:
            return counts
        scope = configured_account_scope(config_id)
        verify_memory_lease()
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            jobs = conn.execute("SELECT * FROM memory_update_jobs WHERE config_id=? AND status='failed' "
                                'AND review_contract_revision<?', (config_id, CONTRACT_REVISION)).fetchall()
            # Recover jobs first: their old scoped projections can attest which
            # account supplied the original evidence before rows are stamped.
            for saved in jobs:
                job = dict(saved)
                if not is_recoverable_contract_failure(job):
                    continue
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_review_results'").fetchone():
                    stored = conn.execute('SELECT status FROM memory_review_results WHERE config_id=? AND operation_id=?',
                                          (config_id, 'run-memory:' + job['run_id'])).fetchone()
                    if stored and stored['status'] == 'partial':
                        continue
                source = job_source(conn, config_id, job['summary_id'], scope, cfg.get('symbol', ''))
                if not source:
                    continue
                try:
                    prior_source = json.loads(job['review_source'])
                except (TypeError, ValueError):
                    prior_source = None
                if prior_source:
                    trusted = prior_source == source
                elif prior_source == {}:
                    witnesses = conn.execute('SELECT DISTINCT account_scope FROM decision_context_projections '
                                             "WHERE config_id=? AND source_kind='strategy_summary' AND source_id=? "
                                             'AND source_hash=? AND projection_version=? AND review_contract_revision<=?',
                                             (config_id, source['source_id'], source['source_hash'],
                                              projections.VERSION, job['review_contract_revision'])).fetchall()
                    trusted = {row['account_scope'] for row in witnesses} == {scope}
                else:
                    trusted = False
                if not trusted:
                    conn.execute('UPDATE memory_update_jobs SET contract_recovery=? WHERE config_id=? AND run_id=?',
                                 (_diagnostic(job, outcome='skipped', reason='source_scope_unverified'), config_id, job['run_id']))
                    continue
                counts['jobs'] += conn.execute("UPDATE memory_update_jobs SET status='pending',attempts=0,next_attempt=0,"
                                               "error='',review_contract_revision=?,contract_recovery=?,review_source=? "
                                               "WHERE config_id=? AND run_id=? AND status='failed' AND review_contract_revision<?",
                                               (CONTRACT_REVISION, _diagnostic(job, outcome='requeued'), json.dumps(source),
                                                config_id, job['run_id'], CONTRACT_REVISION)).rowcount
            rows = conn.execute("SELECT * FROM decision_context_projections WHERE config_id=? AND account_scope=? "
                                "AND projection_version=? AND status='failed' AND review_contract_revision<?",
                                (config_id, scope, projections.VERSION, CONTRACT_REVISION)).fetchall()
            for saved in rows:
                row = dict(saved)
                if not is_recoverable_contract_failure(row) or not projections._original(conn, config_id, row):
                    continue
                counts['projections'] += conn.execute("UPDATE decision_context_projections SET status='pending',attempts=0,"
                                                     "next_attempt=0,error='',review_contract_revision=?,contract_recovery=? WHERE "
                                                     + projections.KEY_WHERE + " AND status='failed' AND review_contract_revision<?",
                                                     (CONTRACT_REVISION, _diagnostic(row, outcome='requeued'),
                                                      *projections.key(config_id, scope, row), CONTRACT_REVISION)).rowcount
            verify_memory_lease()
            conn.commit()
    return counts


def mark_review_attempt(context: dict) -> None:
    from backend.agent import context_projections as projections
    from backend.agent.memory_workflow import verify_memory_lease

    verify_memory_lease()
    with database.get_db_conn() as conn:
        for source in context['sources']:
            conn.execute('UPDATE decision_context_projections SET review_contract_revision=? WHERE '
                         + projections.KEY_WHERE + " AND status='pending'",
                         (CONTRACT_REVISION, *projections.key(context['config_id'], context['account_scope'], source)))
        conn.commit()
