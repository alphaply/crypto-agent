"""Default strategy projections and explicitly requested historical audit text."""
import json
from backend import database
from backend.agent import context_projections as projections


def strategy_memory_rows(rows: list[dict]) -> list[dict]:
    return projections.memory_view_rows(rows)


def memory_audit_detail(memory_id: int) -> dict:
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM short_memories WHERE id=?', (memory_id,)).fetchone()
    if not row:
        raise FileNotFoundError('Memory not found')
    original = dict(row)
    original['source_summary_ids'] = json.loads(original.get('source_summary_ids') or '[]')
    return {'short_memory': original, 'historical': True,
            'as_of': row['window_end'] or row['bucket_end']}
