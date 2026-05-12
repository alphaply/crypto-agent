from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta


def _position_side(order_side) -> str:
    return "LONG" if "BUY" in str(order_side).upper() else "SHORT"


class MockTradingStore:
    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager],
        now_factory,
        epoch_factory: Callable[[], float],
        position_history_upserter: Callable[..., None],
        logger,
    ):
        self._conn_factory = conn_factory
        self._now_factory = now_factory
        self._epoch_factory = epoch_factory
        self._upsert_position_history = position_history_upserter
        self._logger = logger

    def _timestamp(self) -> str:
        return self._now_factory().strftime("%Y-%m-%d %H:%M:%S")

    def _date(self) -> str:
        return self._now_factory().strftime("%Y-%m-%d")

    def _ensure_balance_history_columns(self, cursor) -> None:
        try:
            cursor.execute("ALTER TABLE mock_balance_history ADD COLUMN unrealized_pnl REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE mock_balance_history ADD COLUMN total_equity REAL")
        except Exception:
            pass

    def get_account(self, config_id, symbol):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM mock_accounts WHERE config_id = ?", (config_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    "INSERT INTO mock_accounts (config_id, symbol, balance, failures) VALUES (?, ?, ?, ?)",
                    (config_id, symbol, 10000.0, 0),
                )
                conn.commit()
                return {"config_id": config_id, "symbol": symbol, "balance": 10000.0, "failures": 0}
            return dict(row)

    def update_account_balance(self, config_id, symbol, realized_pnl):
        account = self.get_account(config_id, symbol)
        new_balance = account["balance"] + realized_pnl
        failures = account["failures"]

        if new_balance < 1000:
            new_balance = 10000.0
            failures += 1

        timestamp = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            self._ensure_balance_history_columns(cursor)
            cursor.execute(
                "UPDATE mock_accounts SET balance = ?, failures = ? WHERE config_id = ?",
                (new_balance, failures, config_id),
            )
            cursor.execute(
                "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                (config_id, symbol, timestamp, new_balance, 0.0, new_balance),
            )
            conn.commit()
        return new_balance, failures

    def save_equity_snapshot(self, config_id, symbol, balance, unrealized_pnl):
        try:
            balance = float(balance or 0)
            unrealized_pnl = float(unrealized_pnl or 0)
        except Exception:
            balance = 0.0
            unrealized_pnl = 0.0
        total_equity = balance + unrealized_pnl
        timestamp = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                self._ensure_balance_history_columns(cursor)
                cursor.execute(
                    "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(config_id), symbol, timestamp, balance, unrealized_pnl, total_equity),
                )
                conn.commit()
            except Exception as exc:
                self._logger.error(f"❌ DB Error (save_mock_equity_snapshot): {exc}")
        return total_equity

    def get_equity_history(self, config_id, days=30):
        cutoff = (self._now_factory() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            self._ensure_balance_history_columns(cursor)
            cursor.execute(
                '''
                SELECT date(h.timestamp) as date,
                       h.balance as balance,
                       COALESCE(h.total_equity, h.balance) as equity,
                       COALESCE(h.unrealized_pnl, 0) as unrealized_pnl
                FROM mock_balance_history h
                INNER JOIN (
                    SELECT date(timestamp) as d, MAX(id) as max_id
                    FROM mock_balance_history
                    WHERE config_id = ? AND timestamp >= ?
                    GROUP BY date(timestamp)
                ) latest ON date(h.timestamp) = latest.d AND h.id = latest.max_id
                WHERE h.config_id = ?
                ORDER BY date(h.timestamp) ASC
                ''',
                (config_id, cutoff, config_id),
            )
            rows = [dict(row) for row in cursor.fetchall()]

        if not rows:
            account = self.get_account(config_id, "")
            rows = [{
                "date": self._date(),
                "balance": account["balance"],
                "equity": account["balance"],
                "unrealized_pnl": 0.0,
            }]

        return rows

    def get_orders(self, symbol=None, agent_name=None, config_id=None):
        current_ts = self._epoch_factory()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM mock_orders WHERE status='OPEN' AND (expire_at IS NULL OR expire_at > ?)"
            params = [current_ts]

            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)

            if config_id and agent_name:
                query += " AND (config_id = ? OR agent_name = ?)"
                params.extend([config_id, agent_name])
            elif config_id:
                query += " AND config_id = ?"
                params.append(config_id)
            elif agent_name:
                query += " AND agent_name = ?"
                params.append(agent_name)

            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def create_order(self, symbol, side, price, amount, stop_loss, take_profit, agent_name, config_id=None, order_id=None, expire_at=None):
        if not order_id:
            raise ValueError("order_id must be generated by the caller before store.create_order")

        created_at = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    '''
                    INSERT INTO mock_orders (order_id, symbol, agent_name, config_id, side, price, amount, stop_loss, take_profit, timestamp, expire_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (order_id, symbol, agent_name, config_id or agent_name, side, price, amount, stop_loss, take_profit, created_at, expire_at),
                )
                conn.commit()
                self._upsert_position_history(
                    config_id=config_id or agent_name,
                    symbol=symbol,
                    position_key=str(order_id),
                    side=_position_side(side),
                    status="OPEN",
                    source="mock_order",
                    opened_at=created_at,
                    entry_price=price,
                    amount=amount,
                    raw={"order_id": order_id, "stop_loss": stop_loss, "take_profit": take_profit, "expire_at": expire_at},
                )
            except Exception as exc:
                self._logger.error(f"❌ DB Error (create_mock_order): {exc}")

    def cancel_order(self, order_id):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM mock_orders WHERE order_id = ?", (order_id,))
            deleted = cursor.rowcount > 0
            cursor.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ?", (order_id,))
            conn.commit()
            return deleted

    def mark_order_filled(self, order_id):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE mock_orders SET is_filled = 1 WHERE order_id = ?", (order_id,))
            conn.commit()

    def close_order(self, order_id, close_price=0.0, realized_pnl=0.0):
        closed_position = None
        close_time = self._timestamp()
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM mock_orders WHERE order_id=?", (order_id,)).fetchone()
            if row:
                self.update_account_balance(row["config_id"], row["symbol"], realized_pnl)
                closed_position = dict(row)

            cursor.execute(
                '''
                UPDATE mock_orders
                SET status='CLOSED', close_price=?, realized_pnl=?, close_time=?
                WHERE order_id=? AND status='OPEN'
                ''',
                (close_price, realized_pnl, close_time, order_id),
            )

            cursor.execute("UPDATE orders SET status = 'CLOSED' WHERE order_id = ?", (order_id,))
            conn.commit()

        if closed_position:
            self._upsert_position_history(
                config_id=closed_position.get("config_id"),
                symbol=closed_position.get("symbol"),
                position_key=str(order_id),
                side=_position_side(closed_position.get("side", "")),
                status="CLOSED",
                source="mock_order",
                opened_at=closed_position.get("timestamp"),
                closed_at=close_time,
                entry_price=closed_position.get("price"),
                close_price=close_price,
                amount=closed_position.get("amount"),
                realized_pnl=realized_pnl,
                raw={**closed_position, "close_price": close_price, "realized_pnl": realized_pnl, "close_time": close_time},
            )

    def auto_close_expired_orders(self, config_id=None, symbol=None):
        current_ts = self._epoch_factory()
        close_time = self._timestamp()
        closed_count = 0
        expired_orders = []

        with self._conn_factory() as conn:
            cursor = conn.cursor()
            query = (
                "SELECT order_id, config_id, symbol, side, price, amount, agent_name, timestamp "
                "FROM mock_orders WHERE status='OPEN' "
                "AND COALESCE(is_filled, 0) = 0 "
                "AND expire_at IS NOT NULL AND expire_at <= ?"
            )
            params = [current_ts]
            if config_id:
                query += " AND config_id = ?"
                params.append(config_id)
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            rows = cursor.execute(query, tuple(params)).fetchall()
            expired_orders = [dict(row) for row in rows]

            if expired_orders:
                order_ids = [order["order_id"] for order in expired_orders]
                placeholders = ",".join(["?"] * len(order_ids))
                cursor.execute(
                    f"UPDATE mock_orders SET status='CANCELLED', close_time=? WHERE order_id IN ({placeholders}) AND status='OPEN'",
                    (close_time, *order_ids),
                )
                closed_count = cursor.rowcount
                cursor.execute(
                    f"UPDATE orders SET status='CANCELLED' WHERE order_id IN ({placeholders}) AND status='OPEN'",
                    tuple(order_ids),
                )
                conn.commit()

        for order in expired_orders:
            try:
                self._upsert_position_history(
                    config_id=order.get("config_id"),
                    symbol=order.get("symbol"),
                    position_key=str(order.get("order_id")),
                    side=_position_side(order.get("side", "")),
                    status="CLOSED",
                    source="mock_order",
                    opened_at=order.get("timestamp"),
                    closed_at=close_time,
                    entry_price=order.get("price"),
                    amount=order.get("amount"),
                    realized_pnl=0,
                    raw={**order, "expired": True, "close_time": close_time},
                )
            except Exception as exc:
                self._logger.warning(f"Failed to sync expired mock order position: {exc}")

        return closed_count

    def get_filled_positions(self, config_id, symbol=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            query = (
                "SELECT order_id, symbol, side, price, amount, stop_loss, take_profit FROM mock_orders "
                "WHERE config_id = ? AND status='OPEN' AND COALESCE(is_filled, 0) = 1"
            )
            params = [str(config_id)]
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            rows = cursor.execute(query, tuple(params)).fetchall()
            return [dict(row) for row in rows]
