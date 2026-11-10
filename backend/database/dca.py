from backend import storage
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

    @staticmethod
    def _ensure_symbol_key(conn) -> None:
        """Structure is installed by the explicit PostgreSQL migration."""

    def save_snapshot(self, config_id, symbol, stats) -> None:
        snapshot_date = self._date_factory()
        updated_at = self._timestamp_factory()
        with self._conn_factory() as conn:
            self._ensure_symbol_key(conn)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO dca_daily_snapshots (
                    snapshot_date, config_id, symbol, total_invested, total_qty, avg_cost,
                    buy_count, first_buy, last_buy, actual_balance, updated_at,
                    remaining_cost,market_value,holding_return_pct,realized_pnl,cost_basis_revision,valuation_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(snapshot_date, config_id, symbol) DO UPDATE SET
                    total_invested = excluded.total_invested,
                    total_qty = excluded.total_qty,
                    avg_cost = excluded.avg_cost,
                    buy_count = excluded.buy_count,
                    first_buy = excluded.first_buy,
                    last_buy = excluded.last_buy,
                    actual_balance = excluded.actual_balance,
                    updated_at = excluded.updated_at,
                    remaining_cost=excluded.remaining_cost,market_value=excluded.market_value,
                    holding_return_pct=excluded.holding_return_pct,realized_pnl=excluded.realized_pnl,
                    cost_basis_revision=excluded.cost_basis_revision,valuation_status=excluded.valuation_status
                ''',
                (
                    snapshot_date,
                    str(config_id),
                    str(symbol),
                    float(stats.get("recorded_investment", stats.get("total_invested", 0)) or 0),
                    float(stats.get("total_qty", 0) or 0),
                    float(stats.get("avg_cost", 0) or 0),
                    int(stats.get("buy_count", 0) or 0),
                    stats.get("first_buy"),
                    stats.get("last_buy"),
                    float(stats.get("actual_balance", 0) or 0),
                    updated_at,
                    stats.get('remaining_cost'), stats.get('market_value'), stats.get('return_pct'),
                    stats.get('realized_pnl'), stats.get('cost_basis_revision'), stats.get('valuation_status'),
                ),
            )
            conn.commit()

    def get_snapshot_history(self, config_id, days=30, symbol=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            symbol_clause = ' AND symbol = %s' if symbol else ''
            params = (str(config_id), str(symbol), int(days)) if symbol else (str(config_id), int(days))
            rows = cursor.execute(
                f'''
                SELECT snapshot_date, SUM(total_invested) AS total_invested,
                       CASE WHEN COUNT(*) = 1 THEN MAX(total_qty) END AS total_qty,
                       CASE WHEN COUNT(*) = 1 THEN MAX(avg_cost) END AS avg_cost,
                       SUM(buy_count) AS buy_count,
                       CASE WHEN COUNT(*) = 1 THEN MAX(actual_balance) END AS actual_balance,
                       MAX(updated_at) AS updated_at, COUNT(*) AS symbol_count
                FROM dca_daily_snapshots
                WHERE config_id = %s{symbol_clause}
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC
                LIMIT %s
                ''',
                params,
            ).fetchall()
            # Limit the newest dates first, then display the chart oldest to newest.
            return [storage.record(row) for row in reversed(rows)]
