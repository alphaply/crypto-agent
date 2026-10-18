"""PostgreSQL statistics with the established task/source attribution rules.

Reads borrow one connection and never commit a caller's work. Aggregation stays
in PostgreSQL so the amount of history does not determine Python memory use.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext

from backend import storage


def _pnl_columns(alias: str = "") -> str:
    value = f"{alias}.realized_pnl" if alias else "realized_pnl"
    # PostgreSQL sorts NaN above ordinary numbers; Python's previous comparison
    # counted it as neither a win nor a loss. Keep that distinction and the raw sum.
    return f"""COUNT(*) AS total_trades,
        COALESCE(SUM({value}), 0) AS total_pnl,
        COUNT(*) FILTER (WHERE {value} > 0 AND {value} != 'NaN'::double precision) AS win_count,
        COUNT(*) FILTER (WHERE {value} < 0) AS lose_count"""


def _history_result(row) -> dict:
    total = int(row["total_trades"])
    wins = int(row["win_count"])
    losses = int(row["lose_count"])
    decided = wins + losses
    return {
        "total_trades": total,
        "total_pnl": row["total_pnl"],
        "win_rate": wins / decided * 100 if decided else (None if total else 0),
        "win_count": wins,
        "lose_count": losses,
        "decided_trades": decided,
    }


class TradingStatisticsStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager]):
        self._conn_factory = conn_factory

    def history_for_configs(self, symbol: str, configs: Sequence[tuple[str, str]]) -> dict:
        """Choose each task's original source before combining task totals.

        REAL prefers reconstructed cycles, then closed position observations,
        then fills. A source containing only unknown PnL still takes precedence.
        STRATEGY uses closed mock orders, SPOT_DCA uses fills, and historical
        unknown modes retain their existing mock-plus-fill interpretation.
        """
        if not configs:
            return _history_result({"total_trades": 0, "total_pnl": 0, "win_count": 0, "lose_count": 0})
        with self._conn_factory() as conn:
            sources = []
            args = [[item[0] for item in configs], [item[1] for item in configs]]

            def source(table: str, priority: str, modes: str, *, closed: bool = False,
                       exact_symbol: bool = False) -> None:
                predicate = "h.symbol = %s" if exact_symbol else "(h.symbol = %s OR h.symbol LIKE %s)"
                sources.append(f"""SELECT s.config_id, {priority} AS source_priority, {_pnl_columns('h')}
                    FROM selected s JOIN {table} h ON h.config_id = s.config_id
                    WHERE {modes} AND {predicate}{" AND h.status = 'CLOSED'" if closed else ""}
                    GROUP BY s.config_id, s.mode""")
                args.append(symbol)
                if not exact_symbol:
                    args.append(symbol + "%")

            if any(mode == "REAL" for _, mode in configs):
                if "execution_position_history" in storage.table_names(conn):
                    source("execution_position_history", "0", "s.mode = 'REAL'")
                source("position_history", "1", "s.mode = 'REAL'", closed=True)
            source("trade_history", "CASE WHEN s.mode = 'REAL' THEN 2 ELSE 0 END",
                   "s.mode != 'STRATEGY'")
            source("mock_orders", "0", "s.mode NOT IN ('REAL', 'SPOT_DCA')",
                   closed=True, exact_symbol=True)
            query = """WITH selected AS (
                    SELECT * FROM unnest(%s::text[], %s::text[]) AS tasks(config_id, mode)
                ), totals AS (""" + " UNION ALL ".join(sources) + """), ranked AS (
                    SELECT *, MIN(source_priority) OVER (PARTITION BY config_id) AS preferred
                    FROM totals
                ) SELECT COALESCE(SUM(total_trades), 0) AS total_trades,
                    COALESCE(SUM(total_pnl), 0) AS total_pnl,
                    COALESCE(SUM(win_count), 0) AS win_count,
                    COALESCE(SUM(lose_count), 0) AS lose_count
                FROM ranked WHERE source_priority = preferred"""
            return _history_result(conn.execute(query, args).fetchone())

    def unscoped_history(self, symbol: str) -> dict:
        """Retain the legacy ALL query used when no current tasks exist."""
        with self._conn_factory() as conn:
            row = conn.execute(f"""SELECT {_pnl_columns()} FROM (
                SELECT realized_pnl FROM trade_history WHERE symbol LIKE %s
                UNION ALL
                SELECT realized_pnl FROM mock_orders WHERE symbol = %s AND status = 'CLOSED'
            ) AS observations""", (symbol + "%", symbol)).fetchone()
            return _history_result(row)

    def agent_orders(self, config_id: str, *, connection: storage.Connection | None = None) -> dict:
        """Count all existing order-log categories in one query."""
        with nullcontext(connection) if connection is not None else self._conn_factory() as conn:
            row = conn.execute("""SELECT COUNT(*) AS total_orders,
                COUNT(*) FILTER (WHERE LOWER(side) LIKE '%%buy%%') AS buy_count,
                COUNT(*) FILTER (WHERE LOWER(side) LIKE '%%sell%%') AS sell_count,
                COUNT(*) FILTER (WHERE LOWER(side) LIKE '%%cancel%%') AS cancel_count,
                COUNT(*) FILTER (WHERE LOWER(side) LIKE '%%close%%') AS close_count,
                MIN(timestamp) AS first_order_at, MAX(timestamp) AS last_order_at
                FROM orders WHERE config_id = %s""", (config_id,)).fetchone()
        total, buy, sell = row["total_orders"], row["buy_count"], row["sell_count"]
        ratio = str(round(buy / sell, 2)) if sell else ("∞ (纯多)" if buy else "N/A")
        return {
            "total_orders": total,
            "buy_count": buy,
            "sell_count": sell,
            "cancel_count": row["cancel_count"],
            "close_count": row["close_count"],
            "long_short_ratio": ratio,
            "cancel_rate": round(row["cancel_count"] / total * 100, 1) if total else 0,
            "first_order_at": row["first_order_at"],
            "last_order_at": row["last_order_at"],
        }

    def financial_snapshot(self, symbol: str) -> dict:
        """Load the existing financial page's exact-symbol data, without trading.

        Its win rate uses all fills (unlike history's decided trades), and its
        balance series remains the earliest 200 rows. Formatting belongs to the
        application service; the store does not reinterpret either convention.
        """
        with self._conn_factory() as conn:
            balances = conn.execute("""SELECT timestamp, total_equity, total_balance
                FROM balance_history WHERE symbol = %s ORDER BY id ASC LIMIT 200""", (symbol,)).fetchall()
            daily = conn.execute("""SELECT day, total_equity FROM (
                    SELECT substring(timestamp from 1 for 10) AS day, total_equity,
                        row_number() OVER (PARTITION BY substring(timestamp from 1 for 10)
                            ORDER BY timestamp DESC NULLS LAST) AS rn
                    FROM balance_history WHERE symbol = %s
                ) AS daily_balances WHERE rn = 1 ORDER BY day ASC""", (symbol,)).fetchall()
            totals = conn.execute(f"""SELECT {_pnl_columns()},
                COUNT(*) FILTER (WHERE realized_pnl IS NULL) AS missing_pnl
                FROM trade_history WHERE symbol = %s""", (symbol,)).fetchone()
        if totals["missing_pnl"]:
            # The original financial endpoint failed on a NULL PnL. Do not turn
            # missing financial facts into a successful zero-valued summary.
            raise TypeError("Financial summary contains unknown realized PnL")
        return {
            "balance_history": [storage.record(row) for row in balances],
            "daily_equity": [storage.record(row) for row in daily],
            "totals": storage.record(totals),
        }
