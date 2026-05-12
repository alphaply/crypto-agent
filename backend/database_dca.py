from collections.abc import Callable
from contextlib import AbstractContextManager


class DcaSnapshotStore:
    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager],
        date_factory: Callable[[], str],
        timestamp_factory: Callable[[], str],
    ):
        self._conn_factory = conn_factory
        self._date_factory = date_factory
        self._timestamp_factory = timestamp_factory

    def save_snapshot(self, config_id, symbol, stats) -> None:
        snapshot_date = self._date_factory()
        updated_at = self._timestamp_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO dca_daily_snapshots (
                    snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost,
                    buy_count, first_buy, last_buy, actual_balance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date, config_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    total_invested = excluded.total_invested,
                    total_qty = excluded.total_qty,
                    avg_cost = excluded.avg_cost,
                    buy_count = excluded.buy_count,
                    first_buy = excluded.first_buy,
                    last_buy = excluded.last_buy,
                    actual_balance = excluded.actual_balance,
                    updated_at = excluded.updated_at
                ''',
                (
                    snapshot_date,
                    str(config_id),
                    str(symbol),
                    float(stats.get("total_invested", 0) or 0),
                    float(stats.get("total_qty", 0) or 0),
                    float(stats.get("avg_cost", 0) or 0),
                    int(stats.get("buy_count", 0) or 0),
                    stats.get("first_buy"),
                    stats.get("last_buy"),
                    float(stats.get("actual_balance", 0) or 0),
                    updated_at,
                ),
            )
            conn.commit()

    def get_snapshot_history(self, config_id, days=30):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                '''
                SELECT snapshot_date, total_invested, total_qty, avg_cost, buy_count, actual_balance, updated_at
                FROM dca_daily_snapshots
                WHERE config_id = ?
                ORDER BY snapshot_date ASC
                LIMIT ?
                ''',
                (str(config_id), int(days)),
            ).fetchall()
            return [dict(row) for row in rows]