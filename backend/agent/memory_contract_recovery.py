"""Legacy schema compatibility and source witnesses; no contract recovery jobs."""
from __future__ import annotations



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


def job_source(conn, config_id: str, summary_id: int, scope: str, symbol: str) -> dict | None:
    from backend.agent.context_projections import _source
    from backend.utils.trade_operations import _same_symbol

    row = conn.execute('SELECT * FROM summaries WHERE config_id=? AND id=?', (config_id, summary_id)).fetchone()
    if not row or not _same_symbol(row['symbol'], symbol):
        return None
    source = _source('strategy_summary', dict(row))
    return {'account_scope': scope, 'symbol': row['symbol'], 'source_id': str(summary_id),
            'source_hash': source['source_hash']}


