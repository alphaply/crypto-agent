from backend import storage
"""Read task-wide decisions from every configured member of a spot portfolio."""

from backend.database import get_db_conn


def portfolio_decision_page(symbol: str, portfolio_ids: list[str], config_id: str,
                            page: int, per_page: int) -> tuple[list[dict], int, list[str]]:
    where = '(symbol=%s OR config_id = ANY(%s))'
    params = [symbol, portfolio_ids]
    if config_id and config_id != 'ALL':
        where += ' AND config_id=%s'
        params.append(config_id)
    with get_db_conn() as conn:
        count = conn.execute(f'SELECT COUNT(*) FROM summaries WHERE {where}', params).fetchone()[0]
        rows = conn.execute(f'SELECT * FROM summaries WHERE {where} ORDER BY id DESC LIMIT %s OFFSET %s',
                            [*params, per_page, (max(1, page) - 1) * per_page]).fetchall()
        active = conn.execute('SELECT DISTINCT config_id FROM summaries WHERE (symbol=%s OR config_id = ANY(%s))',
                              [symbol, portfolio_ids]).fetchall()
    return [storage.record(row) for row in rows], count, [row[0] for row in active if row[0]]
