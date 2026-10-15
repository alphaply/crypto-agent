from backend import storage
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
import math
import uuid
import json


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
        """Structure is installed by the explicit PostgreSQL migration."""

    @staticmethod
    def _orders_have_event_type(cursor) -> bool:
        return "event_type" in {row[1] for row in storage.table_columns(cursor.connection, 'orders')}

    def get_account(self, config_id, symbol):
        with self._conn_factory() as conn:
            conn.execute("INSERT INTO mock_accounts (config_id, symbol, balance, failures) VALUES (%s, %s, %s, %s) ON CONFLICT(config_id) DO NOTHING", (config_id, symbol, 10000.0, 0))
            row = conn.execute("SELECT * FROM mock_accounts WHERE config_id = %s", (config_id,)).fetchone()
            conn.commit()
            return storage.record(row)

    def update_account_balance(self, config_id, symbol, realized_pnl):
        with self._conn_factory() as conn:
            storage.lock_scope(conn, 'mock-account', config_id)
            self._credit_balance(conn, config_id, symbol, realized_pnl)
            row = conn.execute("SELECT balance, failures FROM mock_accounts WHERE config_id=%s", (config_id,)).fetchone()
            conn.commit()
            return row['balance'], row['failures']

    def save_equity_snapshot(self, config_id, symbol, balance, unrealized_pnl):
        try:
            balance = float(balance or 0)
            unrealized_pnl = float(unrealized_pnl or 0)
        except storage.Error:
            raise
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
                    "INSERT INTO mock_balance_history (config_id, symbol, timestamp, balance, unrealized_pnl, total_equity) VALUES (%s, %s, %s, %s, %s, %s)",
                    (str(config_id), symbol, timestamp, balance, unrealized_pnl, total_equity),
                )
                conn.commit()
            except storage.Error:
                raise
            except Exception as exc:
                self._logger.error(f"❌ DB Error (save_mock_equity_snapshot): {exc}")
        return total_equity

    def get_equity_history(self, config_id, days=30, symbol=None, include_fallback=True):
        cutoff = (self._now_factory() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            self._ensure_balance_history_columns(cursor)
            symbol_filter = " AND symbol = %s" if symbol else ""
            params = [config_id, cutoff]
            if symbol:
                params.append(symbol)
            params.append(config_id)
            if symbol:
                params.append(symbol)
            cursor.execute(
                f'''
                SELECT substring(h.timestamp from 1 for 10) as date,
                       h.balance as balance,
                       COALESCE(h.total_equity, h.balance) as equity,
                       COALESCE(h.unrealized_pnl, 0) as unrealized_pnl
                FROM mock_balance_history h
                INNER JOIN (
                    SELECT substring(timestamp from 1 for 10) as d, MAX(id) as max_id
                    FROM mock_balance_history
                    WHERE config_id = %s AND timestamp >= %s {symbol_filter}
                    GROUP BY substring(timestamp from 1 for 10)
                ) latest ON substring(h.timestamp from 1 for 10) = latest.d AND h.id = latest.max_id
                WHERE h.config_id = %s {symbol_filter}
                ORDER BY substring(h.timestamp from 1 for 10) ASC
                ''',
                tuple(params),
            )
            rows = [storage.record(row) for row in cursor.fetchall()]

        if not rows and include_fallback:
            account = self.get_account(config_id, symbol or "")
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
            query = "SELECT * FROM mock_orders WHERE status='OPEN' AND (COALESCE(is_filled,0)=1 OR expire_at IS NULL OR expire_at > %s)"
            params = [current_ts]

            if symbol:
                query += " AND symbol = %s"
                params.append(symbol)

            if config_id and agent_name:
                query += " AND (config_id = %s OR agent_name = %s)"
                params.extend([config_id, agent_name])
            elif config_id:
                query += " AND config_id = %s"
                params.append(config_id)
            elif agent_name:
                query += " AND agent_name = %s"
                params.append(agent_name)

            cursor.execute(query, tuple(params))
            return [storage.record(row) for row in cursor.fetchall()]

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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            except storage.Error:
                raise
            except Exception as exc:
                self._logger.error(f"❌ DB Error (create_mock_order): {exc}")

    def cancel_order(self, order_id):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM mock_orders WHERE order_id = %s", (order_id,))
            deleted = cursor.rowcount > 0
            cursor.execute(
                "UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s AND COALESCE(event_type, 'ORDER_CREATED') = 'ORDER_CREATED'",
                (order_id,),
            )
            conn.commit()
            return deleted

    def mark_order_filled(self, order_id):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE mock_orders SET is_filled = 1 WHERE order_id = %s", (order_id,))
            conn.commit()

    def close_order(self, order_id, close_price=0.0, realized_pnl=0.0):
        closed_position = None
        close_time = self._timestamp()
        with self._conn_factory() as conn:
            scope = conn.execute('SELECT config_id FROM mock_orders WHERE order_id=%s', (order_id,)).fetchone()
            if not scope:
                return False
            storage.lock_scope(conn, 'mock-account', scope['config_id'])
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM mock_orders WHERE order_id=%s AND status='OPEN'", (order_id,)).fetchone()
            if row:
                if row['is_filled'] and float(close_price)>0:
                    realized_pnl=(float(close_price)-float(row['price']))*float(row['amount'])*(1 if 'BUY' in row['side'] else -1)
                result={'amount':float(row['amount']),'realized_pnl':realized_pnl,'close_price':close_price}
                self._credit_balance(conn, row['config_id'], row['symbol'], realized_pnl)
                closed_position = storage.record(row)
                history=cursor.execute('SELECT amount FROM position_history WHERE config_id=%s AND position_key=%s',
                                       (row['config_id'],order_id)).fetchone()
                if history and history['amount']:
                    closed_position['amount']=history['amount']
                realized_pnl = float(row['realized_pnl'] or 0) + realized_pnl
            else:
                return False

            cursor.execute(
                '''
                UPDATE mock_orders
                SET status='CLOSED', close_price=%s, realized_pnl=%s, close_time=%s
                WHERE order_id=%s AND status='OPEN'
                ''',
                (close_price, realized_pnl, close_time, order_id),
            )

            if self._orders_have_event_type(cursor):
                cursor.execute(
                    "UPDATE orders SET status = 'CLOSED' WHERE order_id = %s AND COALESCE(event_type, 'ORDER_CREATED') = 'ORDER_CREATED'",
                    (order_id,),
                )
            else:
                cursor.execute("UPDATE orders SET status = 'CLOSED' WHERE order_id = %s", (order_id,))
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
                close_price=(closed_position['price'] + realized_pnl / closed_position['amount'] *
                             (1 if 'BUY' in closed_position['side'] else -1)) if closed_position['amount'] else close_price,
                amount=closed_position.get("amount"),
                realized_pnl=realized_pnl,
                raw={**closed_position, "close_price": close_price, "realized_pnl": realized_pnl, "close_time": close_time},
            )
        return result

    def _credit_balance(self, conn, config_id, symbol, pnl):
        conn.execute('INSERT INTO mock_accounts(config_id,symbol,balance,failures) VALUES(%s,%s,10000,0) ON CONFLICT(config_id) DO NOTHING',
                     (config_id,symbol))
        conn.execute('UPDATE mock_accounts SET balance=balance+%s WHERE config_id=%s',(pnl,config_id))
        conn.execute('UPDATE mock_accounts SET balance=10000,failures=failures+1 WHERE config_id=%s AND balance<1000',(config_id,))
        balance=conn.execute('SELECT balance FROM mock_accounts WHERE config_id=%s',(config_id,)).fetchone()['balance']
        conn.execute('INSERT INTO mock_balance_history(config_id,symbol,timestamp,balance,unrealized_pnl,total_equity) VALUES(%s,%s,%s,%s,0,%s)',
                     (config_id,symbol,self._timestamp(),balance,balance))

    def reduce_positions(self, config_id, symbol, pos_side, amount, current_price, reason='', operation_id=None):
        """Reduce attached simulated exposure proportionally, without FIFO lots.

        Proportional reductions preserve the aggregate average entry cost while
        retaining the existing per-entry attached protection prices.
        """
        if pos_side not in {'LONG','SHORT'} or not math.isfinite(float(amount)) or float(amount)<0:
            raise ValueError('Invalid side or reduction quantity')
        if not math.isfinite(float(current_price)) or float(current_price)<=0:
            raise ValueError('A finite positive current price is required')
        with self._conn_factory() as conn:
            storage.lock_scope(conn, 'mock-account', config_id)
            if operation_id is not None:
                from backend.database_independent import initialize_independent_schema
                initialize_independent_schema(conn.cursor())
                intent = json.dumps({'contract_version': 3, 'action': 'close', 'side': pos_side,
                                     'amount': amount, 'reason': reason}, sort_keys=True)
                old = conn.execute('SELECT result,request_json FROM mock_trade_operations WHERE config_id=%s AND symbol=%s AND operation_id=%s',
                                   (config_id, symbol, operation_id)).fetchone()
                if old:
                    if old['request_json'] != intent:
                        raise ValueError('identity_conflict: operation ID belongs to another simulated close')
                    return json.loads(old['result'])
            rows=conn.execute("SELECT * FROM mock_orders WHERE config_id=%s AND symbol=%s AND side=%s AND status='OPEN' AND is_filled=1 ORDER BY order_id",
                              (config_id,symbol,'BUY' if pos_side=='LONG' else 'SELL')).fetchall()
            total=sum(float(row['amount']) for row in rows)
            if not total:
                result = {'status':'no_position','closed_amount':0,'realized_pnl':0,'orders':[]}
                if operation_id is not None:
                    result.update(status='skipped', contract_version=3, receipt_version=1, operation_id=operation_id)
                    conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(%s,%s,%s,%s,%s)',
                                 (config_id, symbol, operation_id, json.dumps(result), intent))
                    conn.commit()
                return result
            quantity=float(amount) or total
            if quantity>total+1e-12:
                raise ValueError('Reduction quantity exceeds filled exposure')
            proportion=min(quantity/total,1)
            result=[]
            total_pnl=0.0
            event_columns={row[1] for row in storage.table_columns(conn, 'orders')}
            for row in rows:
                reduced=float(row['amount'])*proportion
                remaining=max(float(row['amount'])-reduced,0)
                status='CLOSED' if remaining<=1e-12 else 'OPEN'
                pnl=(float(current_price)-float(row['price']))*reduced*(1 if pos_side=='LONG' else -1)
                cumulative=float(row['realized_pnl'] or 0)+pnl
                total_pnl+=pnl
                conn.execute('UPDATE mock_orders SET amount=%s,status=%s,close_price=%s,realized_pnl=%s,close_time=%s WHERE order_id=%s',
                             (remaining if status=='OPEN' else row['amount'],status,current_price,cumulative,
                              self._timestamp() if status=='CLOSED' else None,row['order_id']))
                if status=='CLOSED':
                    where=" AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'" if 'event_type' in event_columns else ''
                    conn.execute("UPDATE orders SET status='CLOSED' WHERE order_id=%s"+where,(row['order_id'],))
                if 'event_type' in event_columns:
                    conn.execute('''INSERT INTO orders(order_id,timestamp,symbol,agent_name,config_id,trade_mode,side,
                        entry_price,amount,reason,status,event_type,parent_order_id,realized_pnl)
                        VALUES(%s,%s,%s,%s,%s,'STRATEGY',%s,%s,%s,%s,%s,'MANUAL_CLOSE',%s,%s)''',
                        ('reduce:'+uuid.uuid4().hex,self._timestamp(),symbol,config_id,config_id,'CLOSE_'+pos_side,
                         current_price,reduced,reason,'CLOSED',row['order_id'],pnl))
                payload={**storage.record(row),'amount':remaining,'realized_pnl':cumulative}
                history=conn.execute('SELECT amount FROM position_history WHERE config_id=%s AND position_key=%s',
                                     (config_id,row['order_id'])).fetchone()
                history_amount=float(history['amount']) if history and history['amount'] else row['amount']
                conn.execute('''INSERT INTO position_history(config_id,symbol,position_key,side,status,source,opened_at,
                    closed_at,entry_price,close_price,amount,realized_pnl,raw_json,updated_at)
                    VALUES(%s,%s,%s,%s,%s,'mock_order',%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(config_id,position_key) DO UPDATE SET
                    status=excluded.status,closed_at=excluded.closed_at,close_price=excluded.close_price,
                    amount=excluded.amount,realized_pnl=excluded.realized_pnl,raw_json=excluded.raw_json,updated_at=excluded.updated_at''',
                    (config_id,symbol,row['order_id'],pos_side,status,row['timestamp'],
                     self._timestamp() if status=='CLOSED' else None,row['price'],current_price,
                     history_amount,cumulative,json.dumps(payload),self._timestamp()))
                result.append({'order_id':row['order_id'],'closed_amount':reduced,'remaining':remaining,'realized_pnl':pnl})
            self._credit_balance(conn,config_id,symbol,total_pnl)
            receipt = {'status':'closed' if proportion==1 else 'reduced','closed_amount':quantity,
                       'remaining':total-quantity,'realized_pnl':total_pnl,'orders':result}
            if operation_id is not None:
                receipt.update(status='completed', order_status='filled', filled_amount=quantity,
                               price=current_price, contract_version=3, receipt_version=1, operation_id=operation_id)
                conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(%s,%s,%s,%s,%s)',
                             (config_id, symbol, operation_id, json.dumps(receipt), intent))
            conn.commit()
            return receipt

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
                "AND expire_at IS NOT NULL AND expire_at <= %s"
            )
            params = [current_ts]
            if config_id:
                query += " AND config_id = %s"
                params.append(config_id)
            if symbol:
                query += " AND symbol = %s"
                params.append(symbol)
            rows = cursor.execute(query, tuple(params)).fetchall()
            expired_orders = [storage.record(row) for row in rows]

            if expired_orders:
                order_ids = [order["order_id"] for order in expired_orders]
                placeholders = ",".join(["%s"] * len(order_ids))
                cursor.execute(
                    f"UPDATE mock_orders SET status='CANCELLED', close_time=%s WHERE order_id IN ({placeholders}) AND status='OPEN'",
                    (close_time, *order_ids),
                )
                closed_count = cursor.rowcount
                if self._orders_have_event_type(cursor):
                    cursor.execute(
                        f"UPDATE orders SET status='CANCELLED' WHERE order_id IN ({placeholders}) AND status='OPEN' AND COALESCE(event_type, 'ORDER_CREATED') = 'ORDER_CREATED'",
                        tuple(order_ids),
                    )
                else:
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
            except storage.Error:
                raise
            except Exception as exc:
                self._logger.warning(f"Failed to sync expired mock order position: {exc}")

        return closed_count

    def get_filled_positions(self, config_id, symbol=None):
        with self._conn_factory() as conn:
            cursor = conn.cursor()
            query = (
                "SELECT order_id, symbol, side, price, amount, stop_loss, take_profit FROM mock_orders "
                "WHERE config_id = %s AND status='OPEN' AND COALESCE(is_filled, 0) = 1"
            )
            params = [str(config_id)]
            if symbol:
                query += " AND symbol = %s"
                params.append(symbol)
            rows = cursor.execute(query, tuple(params)).fetchall()
            result = [storage.record(row) for row in rows]
            if 'mock_positions' in storage.table_names(conn):
                query='SELECT episode_id AS order_id,symbol,CASE side WHEN \'LONG\' THEN \'BUY\' ELSE \'SELL\' END AS side,entry_price AS price,quantity AS amount,NULL AS stop_loss,NULL AS take_profit FROM mock_positions WHERE config_id=%s AND quantity>0'
                params=[str(config_id)]
                if symbol:
                    query+=' AND symbol=%s'
                    params.append(symbol)
                result.extend(storage.record(row) for row in cursor.execute(query,params))
            return result
