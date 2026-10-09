from collections.abc import Callable
from contextlib import AbstractContextManager

from backend.summary_memory_source import restore_memory_source


class SummaryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], timestamp_factory: Callable[[], str], logger):
        self._conn_factory = conn_factory
        self._timestamp_factory = timestamp_factory
        self._logger = logger

    def save_summary(
        self,
        symbol,
        agent_name,
        content,
        strategy_logic,
        config_id=None,
        agent_type=None,
        reasoning_content=None,
        reasoning_tokens=0,
        report_json=None,
        run_id=None,
        timeframe='1h',
        decision_json=None,
        enqueue_memory=True,
    ):
        timestamp = self._timestamp_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN IMMEDIATE')
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(summaries)").fetchall()}
            for name in ('report_json', 'run_id', 'decision_json'):
                if name not in columns:
                    cursor.execute(f'ALTER TABLE summaries ADD COLUMN {name} TEXT')
            if run_id:
                existing = cursor.execute('SELECT id FROM summaries WHERE run_id=? AND config_id=?',
                                          (run_id, config_id or agent_name)).fetchone()
                if existing:
                    conn.commit()
                    return existing['id']
            if "reasoning_content" in columns and "reasoning_tokens" in columns:
                cursor.execute(
                    '''
                    INSERT INTO summaries (
                        timestamp, symbol, timeframe, agent_name, config_id, agent_type,
                        content, reasoning_content, reasoning_tokens, strategy_logic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        timestamp,
                        symbol,
                        timeframe,
                        agent_name,
                        config_id or agent_name,
                        agent_type,
                        content,
                        reasoning_content or "",
                        max(int(reasoning_tokens or 0), 0),
                        strategy_logic,
                    ),
                )
            elif "reasoning_content" in columns:
                cursor.execute(
                    '''
                    INSERT INTO summaries (
                        timestamp, symbol, timeframe, agent_name, config_id, agent_type,
                        content, reasoning_content, strategy_logic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        timestamp,
                        symbol,
                        timeframe,
                        agent_name,
                        config_id or agent_name,
                        agent_type,
                        content,
                        reasoning_content or "",
                        strategy_logic,
                    ),
                )
            else:
                # Supports lightweight external/test schemas that have not run migrations yet.
                cursor.execute(
                    '''
                    INSERT INTO summaries (
                        timestamp, symbol, timeframe, agent_name, config_id, agent_type,
                        content, strategy_logic
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (timestamp, symbol, timeframe, agent_name, config_id or agent_name, agent_type, content, strategy_logic),
                )
            summary_id = cursor.lastrowid
            cursor.execute('UPDATE summaries SET report_json=?,run_id=?,decision_json=? WHERE id=?',
                           (report_json, run_id, decision_json, summary_id))
            if run_id and enqueue_memory:
                from backend.agent.memory_updates import _initialize
                _initialize(conn)
                cursor.execute('INSERT OR IGNORE INTO memory_update_jobs(config_id,run_id,summary_id) VALUES (?,?,?)',
                               (config_id or agent_name, run_id, summary_id))
            conn.commit()
            return summary_id

    def get_active_agents(self, symbol):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                rows = cursor.execute(
                    "SELECT DISTINCT config_id FROM summaries WHERE symbol = ? AND config_id IS NOT NULL",
                    (symbol,),
                ).fetchall()
                return [row[0] for row in rows if row[0]]
            except Exception:
                return []

    def get_recent_summaries(self, symbol, agent_name=None, limit=10, config_id=None, agent_type=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            if agent_type:
                cursor.execute(
                    '''
                    SELECT * FROM summaries
                    WHERE symbol = ? AND agent_type = ?
                    ORDER BY id DESC LIMIT ?
                    ''',
                    (symbol, agent_type, limit),
                )
            elif config_id:
                cursor.execute(
                    '''
                    SELECT * FROM summaries
                    WHERE symbol = ? AND config_id = ?
                    ORDER BY id DESC LIMIT ?
                    ''',
                    (symbol, config_id, limit),
                )
            elif agent_name:
                cursor.execute(
                    '''
                    SELECT * FROM summaries
                    WHERE symbol = ? AND agent_name = ?
                    ORDER BY id DESC LIMIT ?
                    ''',
                    (symbol, agent_name, limit),
                )
            else:
                cursor.execute(
                    '''
                    SELECT * FROM summaries
                    WHERE symbol = ?
                    ORDER BY id DESC LIMIT ?
                    ''',
                    (symbol, limit),
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_summary_logic(self, config_id, since_time=None, limit=12):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            params = [config_id]
            where = [
                "config_id = ?",
                "(COALESCE(strategy_logic, '') != '' OR COALESCE(content, '') != '')",
            ]
            if since_time:
                where.append("timestamp >= ?")
                params.append(since_time)
            params.append(int(limit or 12))
            rows = cursor.execute(
                f'''
                SELECT timestamp, symbol, config_id, strategy_logic, content AS _source_content
                FROM summaries
                WHERE {' AND '.join(where)}
                ORDER BY id DESC
                LIMIT ?
                ''',
                tuple(params),
            ).fetchall()
            return [restore_memory_source(row) for row in rows]

    def get_summary_count(self, symbol, config_id=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                sql = "SELECT COUNT(*) FROM summaries WHERE symbol = ?"
                params = [symbol]
                if config_id and config_id != "ALL":
                    sql += " AND config_id = ?"
                    params.append(config_id)
                return cursor.execute(sql, tuple(params)).fetchone()[0]
            except Exception:
                return 0

    def get_paginated_summaries(self, symbol, page=1, per_page=10, config_id=None):
        offset = (page - 1) * per_page
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                sql = "SELECT * FROM summaries WHERE symbol = ?"
                params = [symbol]
                if config_id and config_id != "ALL":
                    sql += " AND config_id = ?"
                    params.append(config_id)
                sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
                params.extend([per_page, offset])
                cursor.execute(sql, tuple(params))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as exc:
                self._logger.error(
                    f"Failed to get paginated summaries: symbol={symbol}, page={page}, per_page={per_page}, config_id={config_id}, error={exc}"
                )
                return []

    def delete_by_symbol(self, symbol):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM summaries WHERE symbol = ?", (symbol,))
            summary_count = cursor.rowcount
            # Spot order evidence enforces lifetime spending and cancellation
            # ownership. History cleanup must not reset those safety limits.
            columns = {row[1] for row in cursor.execute('PRAGMA table_info(orders)')}
            spot_filter = " AND COALESCE(trade_mode,'') != 'SPOT_DCA'" if 'trade_mode' in columns else ''
            cursor.execute(f"DELETE FROM orders WHERE symbol = ?{spot_filter}", (symbol,))
            order_count = cursor.rowcount
            cursor.execute("DELETE FROM mock_orders WHERE symbol = ?", (symbol,))
            conn.commit()
            self._logger.info(f"🗑️ Cleaned {symbol}: {summary_count} summaries, {order_count} orders.")
            return summary_count
