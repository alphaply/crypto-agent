from collections.abc import Callable
from contextlib import AbstractContextManager


class BalanceHistoryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], timestamp_factory: Callable[[], str]):
        self._conn_factory = conn_factory
        self._timestamp_factory = timestamp_factory

    def save_snapshot(self, symbol, balance, unrealized_pnl, config_id=None):
        timestamp = self._timestamp_factory()
        equity = float(balance or 0) + float(unrealized_pnl or 0)

        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO balance_history (timestamp, symbol, config_id, total_balance, unrealized_pnl, total_equity)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (timestamp, symbol, str(config_id) if config_id else None, balance, unrealized_pnl, equity),
            )
            conn.commit()

    def get_history(self, symbol, limit=100, config_id=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            if config_id:
                cursor.execute(
                    '''
                    SELECT * FROM balance_history
                    WHERE config_id = ? AND symbol = ?
                    ORDER BY id ASC LIMIT ?
                    ''',
                    (str(config_id), symbol, limit),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                if rows:
                    return rows

            cursor.execute(
                "SELECT * FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT ?",
                (symbol, limit),
            )
            return [dict(row) for row in cursor.fetchall()]