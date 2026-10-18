from backend import storage
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime


def ensure_scoped_trade_ids(conn):
    """The scoped unique key is installed by the versioned migration."""


class TradeHistoryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], logger):
        self._conn_factory = conn_factory
        self._logger = logger

    def save_trades(self, trades, config_id=None):
        if not trades:
            return
        with self._conn_factory() as conn:
            ensure_scoped_trade_ids(conn)
            cursor = conn.cursor()
            for trade in trades:
                conn.execute('SAVEPOINT trade_row')
                try:
                    pnl = trade.get("realizedPnl")
                    if pnl is None and "info" in trade:
                        pnl = trade["info"].get("realizedPnl")
                    if pnl is None:
                        pnl = 0

                    fee_cost = 0
                    fee_currency = ""
                    if trade.get("fee"):
                        fee_cost = float(trade["fee"].get("cost", 0) or 0)
                        fee_currency = trade["fee"].get("currency", "")

                    storage.lock_scope(conn, 'trade-observation', trade['symbol'], str(trade['id']))
                    owner = str(config_id or trade.get("config_id") or "") or None
                    if owner:
                        # Adopt a previously unscoped observation instead of
                        # counting the same fill twice after ownership is known.
                        cursor.execute("""UPDATE trade_history SET config_id=%s
                            WHERE trade_id=%s AND symbol=%s AND COALESCE(config_id,'')=''
                            AND order_id=%s AND NOT EXISTS (SELECT 1 FROM trade_history existing
                                WHERE existing.trade_id=%s AND existing.symbol=%s AND existing.config_id=%s)""",
                            (owner, str(trade['id']), trade['symbol'], str(trade.get('order', trade.get('order_id', ''))),
                             str(trade['id']), trade['symbol'], owner))
                    elif cursor.execute('SELECT 1 FROM trade_history WHERE trade_id=%s AND symbol=%s LIMIT 1',
                                        (str(trade['id']), trade['symbol'])).fetchone():
                        conn.execute('RELEASE SAVEPOINT trade_row')
                        continue

                    cursor.execute(
                        """INSERT INTO trade_history
                        (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(symbol, trade_id, (COALESCE(config_id, ''))) DO NOTHING""",
                        (
                            str(trade["id"]),
                            str(trade.get("order", trade.get("order_id", ""))),
                            owner,
                            datetime.fromtimestamp(trade["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                            trade["symbol"],
                            trade["side"],
                            float(trade["price"]),
                            float(trade["amount"]),
                            float(trade["cost"]),
                            fee_cost,
                            fee_currency,
                            float(pnl),
                        ),
                    )
                    conn.execute('RELEASE SAVEPOINT trade_row')
                except storage.Error:
                    conn.execute('ROLLBACK TO SAVEPOINT trade_row')
                    raise
                except Exception as exc:
                    conn.execute('ROLLBACK TO SAVEPOINT trade_row')
                    conn.execute('RELEASE SAVEPOINT trade_row')
                    self._logger.error(f"Save trade error: {exc}")
            conn.commit()

    def list_trades(self, symbol, limit=50):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trade_history WHERE symbol = %s ORDER BY timestamp DESC NULLS LAST LIMIT %s",
                (symbol, limit),
            )
            return [storage.record(row) for row in cursor.fetchall()]

    def clean_symbol_data(self, symbol):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM balance_history WHERE symbol = %s", (symbol,))
            deleted_balance_rows = cursor.rowcount
            cursor.execute("DELETE FROM trade_history WHERE symbol = %s", (symbol,))
            deleted_trade_rows = cursor.rowcount
            conn.commit()
            return deleted_balance_rows + deleted_trade_rows
