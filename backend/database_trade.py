from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime


def ensure_scoped_trade_ids(conn):
    """Trade IDs are exchange/symbol scoped; preserve legacy rows during upgrade."""
    columns = conn.execute('PRAGMA table_info(trade_history)').fetchall()
    if any(row[1] == 'trade_id' and row[5] for row in columns):
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        columns = conn.execute('PRAGMA table_info(trade_history)').fetchall()
        if any(row[1] == 'trade_id' and row[5] for row in columns):
            conn.execute('CREATE TABLE trade_history_scoped AS SELECT * FROM trade_history')
            conn.execute('DROP TABLE trade_history')
            conn.execute('ALTER TABLE trade_history_scoped RENAME TO trade_history')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_history_scope "
                 "ON trade_history(symbol, trade_id, COALESCE(config_id, ''))")


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

                    owner = str(config_id or trade.get("config_id") or "") or None
                    if owner:
                        # Adopt a previously unscoped observation instead of
                        # counting the same fill twice after ownership is known.
                        cursor.execute("""UPDATE OR IGNORE trade_history SET config_id=?
                            WHERE trade_id=? AND symbol=? AND COALESCE(config_id,'')=''
                            AND order_id=?""", (owner, str(trade['id']), trade['symbol'],
                                                  str(trade.get('order', trade.get('order_id', '')))))
                    elif cursor.execute('SELECT 1 FROM trade_history WHERE trade_id=? AND symbol=? LIMIT 1',
                                        (str(trade['id']), trade['symbol'])).fetchone():
                        continue

                    cursor.execute(
                        '''
                        INSERT OR IGNORE INTO trade_history
                        (trade_id, order_id, config_id, timestamp, symbol, side, price, amount, cost, fee, fee_currency, realized_pnl)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
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
