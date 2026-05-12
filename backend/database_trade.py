from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime


class TradeHistoryStore:
    def __init__(self, conn_factory: Callable[[], AbstractContextManager], logger):
        self._conn_factory = conn_factory
        self._logger = logger

    def save_trades(self, trades, config_id=None):
        if not trades:
            return
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            for trade in trades:
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

                    cursor.execute(
                        '''
                        INSERT OR IGNORE INTO trade_history
                        (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            str(trade["id"]),
                            str(trade.get("order", trade.get("order_id", ""))),
                            str(config_id or trade.get("config_id") or "") or None,
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
                except Exception as exc:
                    self._logger.error(f"Save trade error: {exc}")
            conn.commit()

    def list_trades(self, symbol, limit=50):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trade_history WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def clean_symbol_data(self, symbol):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM balance_history WHERE symbol = ?", (symbol,))
            deleted_balance_rows = cursor.rowcount
            cursor.execute("DELETE FROM trade_history WHERE symbol = ?", (symbol,))
            deleted_trade_rows = cursor.rowcount
            conn.commit()
            return deleted_balance_rows + deleted_trade_rows