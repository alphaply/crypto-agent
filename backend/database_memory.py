from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta


class SummaryMemoryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], now_factory, logger):
        self._conn_factory = conn_factory
        self._now_factory = now_factory
        self._logger = logger

    def _timestamp(self) -> str:
        return self._now_factory().strftime("%Y-%m-%d %H:%M:%S")

    def save_daily_summary(self, date_str, symbol, config_id, summary, source_count):
        created_at = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    '''
                    INSERT INTO daily_summaries (date, symbol, config_id, summary, source_count, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, config_id) DO UPDATE SET
                        summary = excluded.summary,
                        source_count = excluded.source_count,
                        created_at = excluded.created_at
                    ''',
                    (date_str, symbol, config_id, summary, source_count, created_at),
                )
                conn.commit()
            except Exception as exc:
                self._logger.error(f"❌ DB Error (save_daily_summary): {exc}")

    def update_daily_summary(self, date_str, config_id, summary):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE daily_summaries
                SET summary = ?
                WHERE date = ? AND config_id = ?
                ''',
                (summary, date_str, config_id),
            )
            conn.commit()

    def delete_daily_summary(self, date_str, config_id):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM daily_summaries WHERE date = ? AND config_id = ?",
                (date_str, config_id),
            )
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    def get_daily_summaries(self, config_id, days=7):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT date, symbol, config_id, summary, source_count, created_at
                FROM daily_summaries
                WHERE config_id = ?
                ORDER BY date DESC
                LIMIT ?
                ''',
                (config_id, days),
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_daily_summaries(self, symbol=None, config_id=None, days=None, limit=200):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            clauses = []
            params = []
            if symbol and symbol != "ALL":
                clauses.append("symbol = ?")
                params.append(symbol)
            if config_id and config_id != "ALL":
                clauses.append("config_id = ?")
                params.append(config_id)
            if days:
                cutoff = (self._now_factory() - timedelta(days=int(days))).strftime("%Y-%m-%d")
                clauses.append("date >= ?")
                params.append(cutoff)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(int(limit or 200))
            rows = cursor.execute(
                f'''
                SELECT id, date, symbol, config_id, summary, source_count, created_at
                FROM daily_summaries
                {where}
                ORDER BY date DESC, config_id ASC
                LIMIT ?
                ''',
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_pending_daily_summary_data(self, config_id, date_str):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT strategy_logic, timestamp
                FROM summaries
                WHERE config_id = ? AND date(timestamp) = ?
                ORDER BY id ASC
                ''',
                (config_id, date_str),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_summary_logic_between(self, config_id, start_time, end_time):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                '''
                SELECT strategy_logic, timestamp
                FROM summaries
                WHERE config_id = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                  AND strategy_logic IS NOT NULL
                  AND strategy_logic != ''
                ORDER BY id ASC
                ''',
                (config_id, start_time, end_time),
            ).fetchall()
            return [dict(row) for row in rows]

    def save_short_memory(self, bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count):
        created_at = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO short_memories (
                    bucket_start, bucket_end, symbol, config_id, market_summary,
                    position_summary, source_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_id, bucket_start) DO UPDATE SET
                    bucket_end = excluded.bucket_end,
                    symbol = excluded.symbol,
                    market_summary = excluded.market_summary,
                    position_summary = excluded.position_summary,
                    source_count = excluded.source_count,
                    created_at = excluded.created_at
                ''',
                (bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, int(source_count or 0), created_at),
            )
            conn.commit()

    def get_short_memories(self, config_id, limit=2):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                '''
                SELECT bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
                FROM short_memories
                WHERE config_id = ?
                ORDER BY bucket_start DESC
                LIMIT ?
                ''',
                (config_id, int(limit or 2)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_short_memories(self, symbol=None, config_id=None, limit=200):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            sql = '''
                SELECT id, bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
                FROM short_memories
                WHERE 1=1
            '''
            params = []
            if symbol:
                sql += " AND symbol = ?"
                params.append(symbol)
            if config_id and config_id != "ALL":
                sql += " AND config_id = ?"
                params.append(config_id)
            sql += " ORDER BY bucket_start DESC LIMIT ?"
            params.append(int(limit or 200))
            rows = cursor.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def get_short_memory(self, config_id, bucket_start):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                '''
                SELECT bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count, created_at
                FROM short_memories
                WHERE config_id = ? AND bucket_start = ?
                ''',
                (config_id, bucket_start),
            ).fetchone()
            return dict(row) if row else None

    def update_short_memory(self, config_id, bucket_start, market_summary, position_summary):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE short_memories
                SET market_summary = ?, position_summary = ?
                WHERE config_id = ? AND bucket_start = ?
                ''',
                (market_summary, position_summary, config_id, bucket_start),
            )
            conn.commit()
            return cursor.rowcount