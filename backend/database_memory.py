from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
import json

from backend.summary_memory_source import restore_memory_source


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
        days = max(1, int(days or 7))
        cutoff = (self._now_factory() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        today = self._now_factory().strftime("%Y-%m-%d")
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT date, symbol, config_id, summary, source_count, created_at
                FROM daily_summaries
                WHERE config_id = ? AND date >= ? AND date <= ?
                ORDER BY date DESC
                LIMIT ?
                ''',
                (config_id, cutoff, today, days),
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
                SELECT strategy_logic, timestamp, content AS _source_content
                FROM summaries
                WHERE config_id = ? AND date(timestamp) = ?
                ORDER BY id ASC
                ''',
                (config_id, date_str),
            )
            return [restore_memory_source(row) for row in cursor.fetchall()]

    def get_summary_logic_between(self, config_id, start_time, end_time):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            columns = {row[1] for row in cursor.execute('PRAGMA table_info(summaries)')}
            report_field = 'report_json' if 'report_json' in columns else 'NULL AS report_json'
            decision_field = 'decision_json' if 'decision_json' in columns else 'NULL AS decision_json'
            rows = cursor.execute(
                f'''
                SELECT id, strategy_logic, timestamp, content AS _source_content, {report_field}, {decision_field}
                FROM summaries
                WHERE config_id = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                  AND (COALESCE(strategy_logic, '') != '' OR COALESCE(content, '') != '')
                ORDER BY id ASC
                ''',
                (config_id, start_time, end_time),
            ).fetchall()
            result = []
            for row in rows:
                item = restore_memory_source(row)
                from backend.utils.decision_record import append_execution_receipts
                item['strategy_logic'] = append_execution_receipts(item['strategy_logic'], item)
                item.pop('report_json', None)
                item.pop('decision_json', None)
                result.append(item)
            return result

    def save_short_memory(self, bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, source_count,
                          *, window_start=None, window_end=None, source_summary_ids=None, run_id=None):
        created_at = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in cursor.execute('PRAGMA table_info(short_memories)')}
            for name, definition in (('window_start', 'TEXT'), ('window_end', 'TEXT'),
                                     ('source_summary_ids', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('run_id', 'TEXT'), ('version', 'INTEGER')):
                if name not in columns:
                    cursor.execute(f'ALTER TABLE short_memories ADD COLUMN {name} {definition}')
            existing = cursor.execute('SELECT COALESCE(version,id) AS version FROM short_memories WHERE config_id=? AND bucket_start=?',
                                      (config_id, bucket_start)).fetchone()
            version = existing['version'] if existing else cursor.execute(
                'SELECT COALESCE(MAX(COALESCE(version,id)),0)+1 FROM short_memories WHERE config_id=?', (config_id,)).fetchone()[0]
            cursor.execute(
                '''
                INSERT INTO short_memories (
                    bucket_start, bucket_end, symbol, config_id, market_summary,
                    position_summary, source_count, created_at, window_start, window_end,
                    source_summary_ids, run_id, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_id, bucket_start) DO UPDATE SET
                    bucket_end = excluded.bucket_end,
                    symbol = excluded.symbol,
                    market_summary = excluded.market_summary,
                    position_summary = excluded.position_summary,
                    source_count = excluded.source_count,
                    created_at = excluded.created_at,
                    window_start = excluded.window_start,
                    window_end = excluded.window_end,
                    source_summary_ids = excluded.source_summary_ids,
                    run_id = excluded.run_id
                ''',
                (bucket_start, bucket_end, symbol, config_id, market_summary, position_summary, int(source_count or 0), created_at,
                 window_start or bucket_start, window_end or bucket_end, json.dumps(source_summary_ids or []), run_id, version),
            )
            conn.commit()

    @staticmethod
    def _memory_payload(row):
        payload = dict(row)
        payload['window_start'] = payload.get('window_start') or payload['bucket_start']
        payload['window_end'] = payload.get('window_end') or payload['bucket_end']
        payload['version'] = payload.get('version') or payload.get('id')
        try:
            payload['source_summary_ids'] = json.loads(payload.get('source_summary_ids') or '[]')
        except (ValueError, TypeError):
            payload['source_summary_ids'] = []
        return payload

    def get_short_memories(self, config_id, limit=2):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                '''
                SELECT *
                FROM short_memories
                WHERE config_id = ?
                ORDER BY bucket_end DESC, id DESC
                LIMIT ?
                ''',
                (config_id, int(limit or 2)),
            ).fetchall()
            return [self._memory_payload(row) for row in rows]

    def list_short_memories(self, symbol=None, config_id=None, limit=200):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            sql = '''
                SELECT *
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
            sql += " ORDER BY bucket_end DESC, id DESC LIMIT ?"
            params.append(int(limit or 200))
            rows = cursor.execute(sql, tuple(params)).fetchall()
            return [self._memory_payload(row) for row in rows]

    def get_short_memory(self, config_id, bucket_start):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            row = cursor.execute(
                '''
                SELECT *
                FROM short_memories
                WHERE config_id = ? AND bucket_start = ?
                ''',
                (config_id, bucket_start),
            ).fetchone()
            return self._memory_payload(row) if row else None

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

    def delete_short_memories(
        self,
        symbol=None,
        config_id=None,
        bucket_start_from=None,
        bucket_start_to=None,
        buckets=None,
    ):
        where = []
        params = []
        bucket_pairs = [
            (str(item.get("config_id") or "").strip(), str(item.get("bucket_start") or "").strip())
            for item in (buckets or [])
            if isinstance(item, dict)
            and str(item.get("config_id") or "").strip()
            and str(item.get("bucket_start") or "").strip()
        ]

        if bucket_pairs:
            pair_clauses = []
            for pair_config_id, pair_bucket_start in bucket_pairs:
                pair_clauses.append("(config_id = ? AND bucket_start = ?)")
                params.extend([pair_config_id, pair_bucket_start])
            where.append("(" + " OR ".join(pair_clauses) + ")")
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if config_id and config_id != "ALL":
            where.append("config_id = ?")
            params.append(config_id)
        if bucket_start_from:
            where.append("bucket_start >= ?")
            params.append(bucket_start_from)
        if bucket_start_to:
            where.append("bucket_start <= ?")
            params.append(bucket_start_to)
        if not where:
            raise ValueError("At least one short memory delete filter is required")

        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"DELETE FROM short_memories WHERE {' AND '.join(where)}",
                tuple(params),
            )
            conn.commit()
            return cursor.rowcount
