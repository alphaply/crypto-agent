from collections.abc import Callable
from contextlib import AbstractContextManager


class SummaryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], timestamp_factory: Callable[[], str], logger):
        self._conn_factory = conn_factory
        self._timestamp_factory = timestamp_factory
        self._logger = logger

    def save_summary(self, symbol, agent_name, content, strategy_logic, config_id=None, agent_type=None):
        timestamp = self._timestamp_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO summaries (timestamp, symbol, timeframe, agent_name, config_id, agent_type, content, strategy_logic)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (timestamp, symbol, "15m", agent_name, config_id or agent_name, agent_type, content, strategy_logic),
            )
            conn.commit()

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
            cursor.execute("DELETE FROM orders WHERE symbol = ?", (symbol,))
            order_count = cursor.rowcount
            cursor.execute("DELETE FROM mock_orders WHERE symbol = ?", (symbol,))
            conn.commit()
            self._logger.info(f"🗑️ Cleaned {symbol}: {summary_count} summaries, {order_count} orders.")
            return summary_count