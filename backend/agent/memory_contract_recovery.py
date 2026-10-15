"""Legacy schema compatibility and source witnesses; no contract recovery jobs."""
from __future__ import annotations



CONTRACT_REVISION = 2
TABLES = {'decision_context_projections', 'memory_update_jobs'}


def initialize_contract_state(conn, table: str) -> None:
    """Schema is installed by the explicit PostgreSQL migration command."""
    return None


def job_source(conn, config_id: str, summary_id: int, scope: str, symbol: str) -> dict | None:
    from backend.agent.context_projections import _source
    from backend.utils.trade_operations import _same_symbol

    row = conn.execute('SELECT * FROM summaries WHERE config_id=%s AND id=%s', (config_id, summary_id)).fetchone()
    if not row or not _same_symbol(row['symbol'], symbol):
        return None
    source = _source('strategy_summary', dict(row))
    return {'account_scope': scope, 'symbol': row['symbol'], 'source_id': str(summary_id),
            'source_hash': source['source_hash']}


