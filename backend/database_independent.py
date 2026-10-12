"""Transactional aggregate-cost simulated positions and independent exit orders."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from backend.utils.exit_orders import (
    CONDITIONAL_TYPES, LIMIT_TYPES, canonical_order_type, exit_group,
    normalize_close, parse_order_reference, validate_exit_order,
)


ACTIVE_EXIT_STATES = ('OPEN', 'WAITING_TRIGGER')


def _migrate_exit_schema(cursor):
    """Rebuild the legacy NOT NULL quantity table atomically, retaining indexes."""
    columns = {row[1]: row for row in cursor.execute('PRAGMA table_info(mock_exit_orders)')}
    additions = {
        'contract_version': 'INTEGER NOT NULL DEFAULT 1',
        'order_type': 'TEXT', 'quantity_mode': "TEXT NOT NULL DEFAULT 'fixed'",
        'filled_amount': 'REAL NOT NULL DEFAULT 0',
        'unexecuted_amount': 'REAL NOT NULL DEFAULT 0', 'resolved_amount': 'REAL',
        'trigger_basis': "TEXT NOT NULL DEFAULT 'CONTRACT_PRICE'",
        'triggered_at': 'REAL', 'exchange_channel': 'TEXT', 'exchange_order_id': 'TEXT',
        'parent_algo_id': 'TEXT', 'execution_order_id': 'TEXT',
        'replaces_order_id': 'TEXT', 'replacement_order_id': 'TEXT',
    }
    cursor.execute('SAVEPOINT independent_exit_schema')
    try:
        if columns['amount'][3] or columns['remaining'][3]:
            schema = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='mock_exit_orders'").fetchone()[0]
            indexes = [row[0] for row in cursor.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='mock_exit_orders' AND type IN ('index','trigger') AND sql IS NOT NULL")]
            # These two existing columns are the only constraints being relaxed.
            import re
            schema = re.sub(r'\b(amount|remaining)\s+REAL\s+NOT\s+NULL', r'\1 REAL', schema, flags=re.I)
            schema = schema.replace('mock_exit_orders', 'mock_exit_orders_v2', 1)
            cursor.execute(schema)
            names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
            cursor.execute(f'INSERT INTO mock_exit_orders_v2 ({names}) SELECT {names} FROM mock_exit_orders')
            cursor.execute('DROP TABLE mock_exit_orders')
            cursor.execute('ALTER TABLE mock_exit_orders_v2 RENAME TO mock_exit_orders')
            for statement in indexes:
                cursor.execute(statement)
        for name, definition in additions.items():
            if name not in columns:
                cursor.execute(f'ALTER TABLE mock_exit_orders ADD COLUMN {name} {definition}')
        if 'filled_amount' not in columns:
            cursor.execute('UPDATE mock_exit_orders SET filled_amount=MAX(COALESCE(amount,0)-COALESCE(remaining,0),0)')
        cursor.execute("UPDATE mock_exit_orders SET order_type=CASE WHEN contract_version=1 AND exit_type='take_profit_limit' "
                       "THEN 'limit' ELSE exit_type END WHERE order_type IS NULL")
        cursor.execute("UPDATE mock_exit_orders SET exchange_channel=CASE WHEN order_type IN ('market','limit') "
                       "THEN 'regular' ELSE 'algo' END WHERE exchange_channel IS NULL")
        cursor.execute('UPDATE mock_exit_orders SET exchange_order_id=order_id WHERE exchange_order_id IS NULL')
        cursor.execute('RELEASE independent_exit_schema')
    except Exception:
        cursor.execute('ROLLBACK TO independent_exit_schema')
        cursor.execute('RELEASE independent_exit_schema')
        raise


def initialize_independent_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_positions (
        config_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
        episode_id TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0,
        entry_price REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0,
        opened_amount REAL NOT NULL DEFAULT 0,
        opened_at TEXT, updated_at TEXT, status TEXT NOT NULL DEFAULT 'WAITING',
        PRIMARY KEY(config_id,symbol,side))''')
    if 'opened_amount' not in {row[1] for row in cursor.execute('PRAGMA table_info(mock_positions)')}:
        cursor.execute('ALTER TABLE mock_positions ADD COLUMN opened_amount REAL NOT NULL DEFAULT 0')
    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_exit_orders (
        order_id TEXT PRIMARY KEY, config_id TEXT NOT NULL, symbol TEXT NOT NULL,
        pos_side TEXT NOT NULL, episode_id TEXT NOT NULL, exit_type TEXT NOT NULL,
        price REAL, trigger_price REAL, amount REAL, remaining REAL,
        status TEXT NOT NULL, reason TEXT, timestamp TEXT NOT NULL,
        created_at REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_trade_operations (
        config_id TEXT NOT NULL, symbol TEXT NOT NULL, operation_id TEXT NOT NULL,
        result TEXT NOT NULL, PRIMARY KEY(config_id,symbol,operation_id))''')
    operation_columns = {row[1] for row in cursor.execute('PRAGMA table_info(mock_trade_operations)')}
    if 'request_json' not in operation_columns:
        cursor.execute('ALTER TABLE mock_trade_operations ADD COLUMN request_json TEXT')
    _migrate_exit_schema(cursor)
    columns = {row[1] for row in cursor.execute('PRAGMA table_info(mock_orders)')}
    for name in ('execution_mode', 'episode_id'):
        if name not in columns:
            cursor.execute(f'ALTER TABLE mock_orders ADD COLUMN {name} TEXT')
    if 'activated_at' not in columns:
        cursor.execute('ALTER TABLE mock_orders ADD COLUMN activated_at REAL')
    if 'contract_version' not in columns:
        cursor.execute('ALTER TABLE mock_orders ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1')
    if 'filled_amount' not in columns:
        cursor.execute('ALTER TABLE mock_orders ADD COLUMN filled_amount REAL NOT NULL DEFAULT 0')
        cursor.execute('UPDATE mock_orders SET filled_amount=amount WHERE is_filled=1')


class MockIndependentTrading:
    def __init__(self, config_id, symbol):
        self.config_id, self.symbol = str(config_id), symbol

    def _now(self):
        from backend.database import _current_timestamp
        return _current_timestamp()

    def _position(self, conn, side):
        row = conn.execute('SELECT * FROM mock_positions WHERE config_id=? AND symbol=? AND side=?',
                           (self.config_id, self.symbol, side)).fetchone()
        return dict(row) if row else None

    def _replay(self, conn, operation_id, request=None):
        if not operation_id:
            return None
        row = conn.execute('SELECT result,request_json FROM mock_trade_operations WHERE config_id=? AND symbol=? AND operation_id=?',
                           (self.config_id, self.symbol, operation_id)).fetchone()
        encoded = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False) if request is not None else None
        if row:
            if row['request_json'] is not None and row['request_json'] != encoded:
                raise ValueError('identity_conflict: operation ID belongs to a different request')
            return json.loads(row['result'])
        conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(?,?,?,?,?)',
                     (self.config_id, self.symbol, operation_id, '{}', encoded))
        return None

    def _finish(self, conn, result, operation_id=None):
        if operation_id:
            conn.execute('UPDATE mock_trade_operations SET result=? WHERE config_id=? AND symbol=? AND operation_id=?',
                         (json.dumps(result), self.config_id, self.symbol, operation_id))
        conn.commit()
        return result

    def _available(self, conn):
        account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (self.config_id,)).fetchone()
        balance = float(account['balance']) if account else 10000.0
        pending = conn.execute("SELECT COALESCE(SUM(price*CASE WHEN execution_mode='independent_exits' THEN MAX(amount-filled_amount,0) ELSE amount END),0) FROM mock_orders WHERE config_id=? AND status='OPEN' "
                               "AND (COALESCE(is_filled,0)=1 OR expire_at IS NULL OR expire_at>?)",
                               (self.config_id, time.time())).fetchone()[0]
        held = conn.execute('SELECT COALESCE(SUM(quantity*entry_price),0) FROM mock_positions WHERE config_id=?',
                            (self.config_id,)).fetchone()[0]
        return max(balance - float(pending) - float(held), 0.0)

    def _log(self, conn, order_id, side, amount, price, event, reason='', pnl=0, parent=None, status='OPEN'):
        conn.execute('''INSERT INTO orders(order_id,timestamp,symbol,agent_name,config_id,trade_mode,
            side,entry_price,amount,reason,status,event_type,parent_order_id,realized_pnl)
            VALUES(?,?,?,?,?,'STRATEGY',?,?,?,?,?,?,?,?)''',
            (order_id, self._now(), self.symbol, self.config_id, self.config_id,
             side, price, amount, reason, status, event, parent, pnl))

    def _history(self, conn, position, close_price=None):
        conn.execute('''INSERT INTO position_history(config_id,symbol,position_key,side,status,source,
            opened_at,closed_at,entry_price,close_price,amount,realized_pnl,raw_json,updated_at)
            VALUES(?,?,?,?,?,'mock_aggregate',?,?,?,?,?,?,?,?)
            ON CONFLICT(config_id,position_key) DO UPDATE SET status=excluded.status,
            closed_at=excluded.closed_at,entry_price=excluded.entry_price,close_price=COALESCE(excluded.close_price,position_history.close_price),
            amount=excluded.amount,realized_pnl=excluded.realized_pnl,raw_json=excluded.raw_json,updated_at=excluded.updated_at''',
            (self.config_id, self.symbol, position['episode_id'], position['side'],
             'CLOSED' if position['status'] == 'DONE' else 'OPEN', position['opened_at'],
             self._now() if position['status'] == 'DONE' else None, position['entry_price'], close_price,
             position.get('opened_amount', position['quantity']), position['realized_pnl'], json.dumps(position), self._now()))

    def open(self, op, operation_id=None, contract_version=1):
        from backend import database
        from backend.utils.independent_exits import positive
        if op.stop_loss is not None or op.take_profit is not None:
            raise ValueError('Independent entries do not accept attached TP/SL')
        if op.action not in ('BUY_LIMIT', 'SELL_LIMIT'):
            raise ValueError('Independent entries require a limit order and explicit direction')
        amount, price = positive(op.amount, 'amount'), positive(op.entry_price, 'entry price')
        side = 'LONG' if op.action == 'BUY_LIMIT' else 'SHORT'
        request = dict(action='open', contract_version=contract_version, side=side, amount=amount,
                       price=price, reason=op.reason, valid_duration_hours=getattr(op, 'valid_duration_hours', 24))
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay = self._replay(conn, operation_id, request)
            if replay is not None:
                return replay
            if conn.execute("SELECT 1 FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN' "
                            "AND COALESCE(execution_mode,'')!='independent_exits' LIMIT 1",
                            (self.config_id, self.symbol)).fetchone():
                raise ValueError('An attached-mode simulated cycle remains active')
            if amount * price > self._available(conn) + 1e-9:
                raise ValueError('Order value exceeds available simulated funds')
            conn.execute('INSERT OR IGNORE INTO mock_accounts(config_id,symbol,balance,failures) VALUES(?,?,10000,0)',
                         (self.config_id, self.symbol))
            position = self._position(conn, side)
            if not position or position['status'] == 'DONE':
                episode = uuid.uuid4().hex
                conn.execute('''INSERT INTO mock_positions(config_id,symbol,side,episode_id,opened_at,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(config_id,symbol,side) DO UPDATE SET
                    episode_id=excluded.episode_id,quantity=0,entry_price=0,realized_pnl=0,opened_amount=0,
                    opened_at=excluded.opened_at,updated_at=excluded.updated_at,status='WAITING' ''',
                    (self.config_id, self.symbol, side, episode, self._now(), self._now()))
            else:
                episode = position['episode_id']
            oid = 'ST-' + uuid.uuid4().hex[:16]
            expiry = time.time() + int(getattr(op, 'valid_duration_hours', 24)) * 3600
            conn.execute('''INSERT INTO mock_orders(order_id,timestamp,symbol,agent_name,config_id,side,
                price,amount,expire_at,status,is_filled,execution_mode,episode_id,activated_at,contract_version)
                VALUES(?,?,?,?,?,?,?,?,?,'OPEN',0,'independent_exits',?,?,?)''',
                (oid, self._now(), self.symbol, self.config_id, self.config_id,
                 'BUY' if side == 'LONG' else 'SELL', price, amount, expiry, episode,time.time(),contract_version))
            self._log(conn, oid, 'BUY' if side == 'LONG' else 'SELL', amount, price, 'ORDER_CREATED', op.reason)
            row = dict(conn.execute('SELECT * FROM mock_orders WHERE order_id=?', (oid,)).fetchone())
            return self._finish(conn, self._entry_result(row), operation_id)

    def _exit_row(self, conn, order_id):
        channel, raw = parse_order_reference(order_id)
        rows = conn.execute('SELECT * FROM mock_exit_orders WHERE (order_id=? OR execution_order_id=?) AND config_id=? AND symbol=?',
                            (raw, raw, self.config_id, self.symbol)).fetchall()
        rows = [dict(row) for row in rows if channel is None or
                (row['execution_order_id'] == raw and channel == 'regular') or
                (row['order_id'] == raw and row['exchange_channel'] == channel)]
        if len(rows) > 1:
            raise ValueError('ambiguous_order_id: use a channel-qualified order_id')
        if not rows:
            raise ValueError('Exit order is not owned by this configuration')
        return rows[0]

    def _reference(self, row):
        raw = row.get('execution_order_id') or row['order_id']
        if row.get('contract_version', 1) == 1:
            return raw
        channel = 'regular' if row.get('execution_order_id') else row.get('exchange_channel', 'regular')
        return f'{channel}:{raw}'

    def _entry_result(self, row):
        filled = float(row.get('filled_amount') or 0)
        state = 'filled' if row['status'] == 'FILLED' else 'canceled' if row['status'] == 'CANCELLED' else 'partially_filled' if filled else 'open'
        result = dict(row, id=row['order_id'], order_id=self._reference(row),
                      kind='entry', order_type='limit', exchange_channel='regular',
                      quantity_mode='fixed', filled_amount=filled, filled=filled,
                      remaining=max(row['amount']-filled, 0), remaining_amount=max(row['amount']-filled, 0),
                      order_status=state, pending=False)
        result['pos_side']='LONG' if row['side']=='BUY' else 'SHORT'
        result['status'] = ('submitted' if state in ('open', 'partially_filled') else 'completed') if row.get('contract_version', 1) == 2 else row['status'].lower()
        return result

    def _result(self, row):
        order_type = canonical_order_type(row)
        actual_remaining = row['remaining']
        if row['quantity_mode']=='all':
            actual_remaining = max(row['resolved_amount']-row['filled_amount'],0) if row['resolved_amount'] is not None else None
        state = row['status'].lower()
        if state == 'open' and order_type in CONDITIONAL_TYPES and not row.get('triggered_at'):
            state = 'waiting_trigger'
        elif state == 'closed':
            state = 'filled'
        elif state == 'cancelled':
            state = 'canceled'
        elif state == 'open' and row.get('filled_amount', 0):
            state = 'partially_filled'
        result = dict(row, id=row['order_id'], order_id=self._reference(row), kind='exit',
                      order_type=order_type, order_status=state, pending=False,
                      requested_amount=row['amount'], remaining_amount=actual_remaining,
                      actual_remaining=actual_remaining, filled=row['filled_amount'],
                      status=row['status'].lower())
        result.update(side=row['pos_side'],
                      parent_order_id='algo:'+row['order_id'] if row['execution_order_id'] else None,
                      exchange_order_id=row['execution_order_id'] or row['order_id'],
                      exchange_channel='regular' if row['execution_order_id'] else row['exchange_channel'])
        if row.get('contract_version', 1) == 2:
            result['status'] = 'submitted' if row['status'] in ACTIVE_EXIT_STATES else 'completed'
        return result

    def close(self, pos_side, amount, exit_type, current_price, price=None, trigger_price=None,
              reason='', operation_id=None, contract_version=1, close_all=False):
        from backend import database
        normalized = normalize_close(exit_type, amount, close_all, contract_version)
        order_type = normalized['order_type']
        request = dict(action='close', contract_version=contract_version, side=pos_side, amount=amount,
                       exit_type=exit_type, close_all=close_all, price=price, trigger_price=trigger_price, reason=reason)
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay = self._replay(conn, operation_id, request)
            if replay is not None:
                return replay
            validate_exit_order(order_type, pos_side, current_price, price, trigger_price)
            position = self._position(conn, pos_side)
            if not position or position['quantity'] <= 0:
                raise ValueError('Exit orders require an already filled position')
            all_mode = normalized['quantity_mode'] == 'all'
            quantity = None if all_mode else normalized['amount'] or position['quantity']
            if quantity is not None and quantity > position['quantity'] + 1e-12:
                raise ValueError('Exit quantity exceeds position')
            oid = 'SX-' + uuid.uuid4().hex[:16]
            conditional = order_type in CONDITIONAL_TYPES
            conn.execute('''INSERT INTO mock_exit_orders(order_id,config_id,symbol,pos_side,episode_id,exit_type,
                price,trigger_price,amount,remaining,status,reason,timestamp,created_at,
                contract_version,order_type,quantity_mode,exchange_channel,exchange_order_id,resolved_amount)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (oid,self.config_id,self.symbol,pos_side,position['episode_id'],exit_type,price,trigger_price,
                 quantity,quantity,'WAITING_TRIGGER' if conditional and contract_version==2 else 'OPEN',
                 reason,self._now(),time.time(),contract_version,order_type,normalized['quantity_mode'],
                 'algo' if conditional else 'regular',oid,position['quantity'] if all_mode and order_type=='market' else None))
            self._log(conn, oid, 'CLOSE_' + pos_side, quantity, price or trigger_price or current_price,
                      'CLOSE_ORDER_CREATED', reason, parent=position['episode_id'])
            if order_type == 'market':
                self._fill_exit(conn, self._exit_row(conn, oid), current_price)
            return self._finish(conn, self._result(self._exit_row(conn, oid)), operation_id)

    def _cleanup_cycle(self, conn, position):
        """A verified local zero ends only this direction's current episode."""
        if position['quantity'] > 1e-12:
            return
        conn.execute("UPDATE mock_exit_orders SET status='CANCELLED',unexecuted_amount=COALESCE(remaining,0) "
                     "WHERE config_id=? AND symbol=? AND episode_id=? AND status IN ('OPEN','WAITING_TRIGGER')",
                     (self.config_id, self.symbol, position['episode_id']))
        entries = conn.execute("SELECT order_id FROM mock_orders WHERE config_id=? AND symbol=? AND episode_id=? AND status='OPEN'",
                               (self.config_id, self.symbol, position['episode_id'])).fetchall()
        conn.execute("UPDATE mock_orders SET status='CANCELLED',close_time=? WHERE config_id=? AND symbol=? AND episode_id=? AND status='OPEN'",
                     (self._now(), self.config_id, self.symbol, position['episode_id']))
        for entry in entries:
            self._log(conn, entry['order_id'], 'CANCEL', 0, 0, 'CANCELLED', 'Position cycle ended', status='CANCELLED')

    def _resize(self, conn, position):
        # Kept for old callers; fixed orders are never automatically reallocated.
        self._cleanup_cycle(conn, position)

    def _fill_exit(self, conn, row, fill_price):
        # The same write transaction checks the current order/position and updates
        # balances, quantity and events; repeated monitor passes cannot realize twice.
        live = self._exit_row(conn, row['order_id'])
        if live['status'] not in ACTIVE_EXIT_STATES:
            return False
        position = self._position(conn, live['pos_side'])
        if not position or position['episode_id'] != live['episode_id'] or position['quantity'] <= 0:
            conn.execute("UPDATE mock_exit_orders SET status='CANCELLED',unexecuted_amount=COALESCE(remaining,0) WHERE order_id=?", (live['order_id'],))
            return False
        target = live['resolved_amount'] if live['order_type']=='market' and live['quantity_mode']=='all' else position['quantity'] if live['quantity_mode']=='all' else live['remaining']
        quantity = min(target, position['quantity'])
        unexecuted = max(target-quantity, 0)
        pnl = (fill_price-position['entry_price']) * quantity * (1 if position['side'] == 'LONG' else -1)
        remaining = max(position['quantity']-quantity, 0)
        state = 'DONE' if remaining <= 1e-12 else 'ACTIVE'
        if state == 'DONE':
            remaining = 0
        conn.execute('UPDATE mock_positions SET quantity=?,realized_pnl=realized_pnl+?,status=?,updated_at=? '
                     'WHERE config_id=? AND symbol=? AND side=?',
                     (remaining,pnl,state,self._now(),self.config_id,self.symbol,position['side']))
        conn.execute("UPDATE mock_exit_orders SET status=?,remaining=?,filled_amount=filled_amount+?,"
                     "unexecuted_amount=?,resolved_amount=COALESCE(resolved_amount,?),realized_pnl=realized_pnl+? WHERE order_id=?",
                     ('CANCELLED' if unexecuted>1e-12 else 'CLOSED',None if live['quantity_mode']=='all' else unexecuted,
                      quantity,unexecuted,quantity,pnl,live['order_id']))
        conn.execute('UPDATE mock_accounts SET balance=balance+? WHERE config_id=?', (pnl,self.config_id))
        # Preserve existing insolvency reset behavior without a separate connection.
        conn.execute('UPDATE mock_accounts SET balance=10000,failures=failures+1 WHERE config_id=? AND balance<1000', (self.config_id,))
        account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (self.config_id,)).fetchone()
        conn.execute('INSERT INTO mock_balance_history(config_id,symbol,timestamp,balance,unrealized_pnl,total_equity) VALUES(?,?,?,?,0,?)',
                     (self.config_id,self.symbol,self._now(),account['balance'],account['balance']))
        self._log(conn, 'fill:' + uuid.uuid4().hex, 'CLOSE_' + position['side'], quantity,fill_price,
                  'MANUAL_CLOSE' if live['order_type']=='market' else 'SL_HIT' if exit_group(live)=='stop_loss' else 'TP_HIT',
                  live['reason'],pnl,parent=live['order_id'],status='CLOSED')
        position.update(quantity=remaining,realized_pnl=position['realized_pnl']+pnl,status=state)
        self._cleanup_cycle(conn, position)
        self._history(conn,position,fill_price)
        return True

    def amend_entry(self, order_id, entry_price=None, amount=None, reason='', operation_id=None, pos_side=None):
        return self._edit(order_id, amount, entry_price, None, reason, operation_id,
                          None, legacy='entry', pos_side=pos_side)

    def amend_exit(self, order_id, amount=None, price=None, trigger_price=None, reason='',
                   operation_id=None, current_price=None):
        return self._edit(order_id, amount, price, trigger_price, reason, operation_id,
                          current_price, legacy='exit')

    def edit(self, order_id, remaining_amount=None, price=None, trigger_price=None, reason='',
             operation_id=None, current_price=None):
        return self._edit(order_id, remaining_amount, price, trigger_price, reason,
                          operation_id, current_price)

    def _entry_row(self, conn, order_id):
        channel, raw = parse_order_reference(order_id)
        if channel not in (None, 'regular'):
            return None
        row = conn.execute("SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=? "
                           "AND execution_mode='independent_exits'", (raw,self.config_id,self.symbol)).fetchone()
        return dict(row) if row else None

    def describe_order(self, order_id):
        from backend import database
        with database.get_db_conn() as conn:
            entry = self._entry_row(conn, order_id)
            try:
                exit_row = self._exit_row(conn, order_id)
            except ValueError as exc:
                if 'ambiguous' in str(exc):
                    raise
                exit_row = None
            if entry and exit_row:
                raise ValueError('ambiguous_order_id: use a channel-qualified order_id')
            if entry:
                result = self._entry_result(entry)
                result['side'] = 'LONG' if entry['side']=='BUY' else 'SHORT'
            elif exit_row:
                result = self._result(exit_row)
                result['side'] = exit_row['pos_side']
            else:
                raise ValueError('Order is not owned by this configuration')
            return result

    def _edit(self, order_id, remaining_amount, price, trigger_price, reason,
              operation_id, current_price, legacy=None, pos_side=None):
        from backend import database
        from backend.utils.exit_orders import positive_number
        if remaining_amount is None and price is None and trigger_price is None:
            raise ValueError('At least one editable field is required')
        if remaining_amount is not None:
            positive_number(remaining_amount, 'remaining amount' if not legacy else 'amount')
        request = dict(action='amend_'+legacy if legacy else 'edit', contract_version=1 if legacy else 2,
                       order_id=order_id, amount=remaining_amount, price=price, trigger_price=trigger_price,
                       reason=reason, pos_side=pos_side)
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay=self._replay(conn,operation_id,request)
            if replay is not None:
                return replay
            entry = self._entry_row(conn, order_id)
            if entry and parse_order_reference(order_id)[0] is None:
                try:
                    self._exit_row(conn, order_id)
                except ValueError as exc:
                    if 'ambiguous' in str(exc):
                        raise
                else:
                    raise ValueError('ambiguous_order_id: use a channel-qualified order_id')
            if entry and legacy!='exit':
                if entry['status']!='OPEN':
                    raise ValueError('No owned pending entry to amend')
                if trigger_price is not None:
                    raise ValueError('Regular orders do not accept trigger_price')
                if pos_side is not None and pos_side != ('LONG' if entry['side']=='BUY' else 'SHORT'):
                    raise ValueError('Entry amendment cannot change position direction')
                filled = entry['filled_amount']
                remaining = entry['amount']-filled if remaining_amount is None else remaining_amount-filled if legacy=='entry' else remaining_amount
                positive_number(remaining, 'remaining amount')
                new_price=entry['price'] if price is None else positive_number(price,'price')
                if new_price*remaining > self._available(conn)+entry['price']*(entry['amount']-filled)+1e-9:
                    raise ValueError('Amended entry exceeds available simulated funds')
                conn.execute('UPDATE mock_orders SET price=?,amount=?,activated_at=? WHERE order_id=?',
                             (new_price,filled+remaining,time.time(),entry['order_id']))
                self._log(conn,'edit:'+uuid.uuid4().hex,entry['side'],remaining,new_price,'ORDER_AMENDED',reason,parent=entry['order_id'])
                result=self._entry_result(self._entry_row(conn,entry['order_id']))
                result.update(edit_method='native')
                if not legacy:
                    result.update(status='completed', order_id='regular:'+entry['order_id'])
                else:
                    result['status']='open'
                return self._finish(conn,result,operation_id)
            if legacy=='entry':
                raise ValueError('No owned pending entry to amend')
            row=self._exit_row(conn,order_id)
            channel, raw = parse_order_reference(order_id)
            if row['execution_order_id'] and raw==row['order_id']:
                result=self._result(row)
                result.update(status='failed',error='order_already_triggered',code='order_already_triggered',
                              order_id='regular:'+row['execution_order_id'],previous_order_id=order_id)
                return self._finish(conn,result,operation_id)
            if row['status'] not in ACTIVE_EXIT_STATES or row['order_type']=='market':
                raise ValueError('Only open limit or conditional exits may be amended')
            conditional=row['order_type'] in CONDITIONAL_TYPES and not row['triggered_at']
            if not conditional and trigger_price is not None:
                raise ValueError('Regular orders do not accept trigger_price')
            if row['quantity_mode']=='all' and remaining_amount is not None:
                raise ValueError('all orders do not accept remaining_amount')
            position=self._position(conn,row['pos_side'])
            if conditional and current_price is None:
                raise ValueError('A fresh current_price is required to validate an exit amendment')
            new_price=row['price'] if price is None else positive_number(price,'price')
            new_trigger=row['trigger_price'] if trigger_price is None else positive_number(trigger_price,'trigger price')
            validate_exit_order(row['order_type'] if conditional else 'limit',row['pos_side'],
                                current_price or position['entry_price'],new_price,new_trigger if conditional else None)
            remaining=row['remaining'] if remaining_amount is None else remaining_amount
            if remaining is not None and remaining>position['quantity']+1e-12:
                raise ValueError('Amended exit quantity exceeds position')
            old_reference=self._reference(row)
            if conditional:
                new=dict(row, order_id='SX-'+uuid.uuid4().hex[:16], price=new_price, trigger_price=new_trigger,
                         amount=remaining, remaining=remaining, reason=reason, created_at=time.time(),
                         timestamp=self._now(), filled_amount=0, unexecuted_amount=0, realized_pnl=0,
                         replaces_order_id=row['order_id'], replacement_order_id=None, resolved_amount=None)
                new['exchange_order_id']=new['order_id']
                conn.execute("UPDATE mock_exit_orders SET status='CANCELLED',unexecuted_amount=COALESCE(remaining,0),replacement_order_id=? WHERE order_id=?",
                             (new['order_id'],row['order_id']))
                columns=','.join(new)
                conn.execute(f"INSERT INTO mock_exit_orders({columns}) VALUES({','.join('?' for _ in new)})",tuple(new.values()))
                row=new
            else:
                total=row['filled_amount']+remaining
                conn.execute('UPDATE mock_exit_orders SET price=?,amount=?,remaining=?,reason=?,created_at=?,timestamp=? WHERE order_id=?',
                             (new_price,total,remaining,reason,time.time(),self._now(),row['order_id']))
                row=self._exit_row(conn,row['order_id'])
            self._log(conn,'edit:'+uuid.uuid4().hex,'CLOSE_'+row['pos_side'],remaining,new_price or new_trigger,
                      'ORDER_AMENDED',reason,parent=row['order_id'])
            result=self._result(row)
            result.update(edit_method='cancel_replace' if conditional else 'native', previous_order_id=old_reference)
            if not legacy:
                result['status']='completed'
                result['order_id']=('regular:' if row['execution_order_id'] else row['exchange_channel']+':')+(row['execution_order_id'] or row['order_id'])
            else:
                result['status']=row['status'].lower()
            return self._finish(conn,result,operation_id)

    def cancel(self, order_id, operation_id=None):
        from backend import database
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay=self._replay(conn,operation_id,dict(action='cancel',order_id=order_id))
            if replay is not None:
                return replay
            try:
                row=self._exit_row(conn,order_id)
            except ValueError as exc:
                if 'ambiguous' in str(exc):
                    raise
                row=None
            if row:
                if parse_order_reference(order_id)[0] is None and self._entry_row(conn,order_id):
                    raise ValueError('ambiguous_order_id: use a channel-qualified order_id')
                if row['status'] in ACTIVE_EXIT_STATES:
                    conn.execute("UPDATE mock_exit_orders SET status='CANCELLED',unexecuted_amount=COALESCE(remaining,0) WHERE order_id=?",(row['order_id'],))
                else:
                    return self._finish(conn,self._result(dict(row)),operation_id)
                result=self._result(self._exit_row(conn,row['order_id']))
            else:
                row=self._entry_row(conn,order_id)
                if not row or row['is_filled']:
                    raise ValueError('No owned pending order to cancel')
                conn.execute("UPDATE mock_orders SET status='CANCELLED',close_time=? WHERE order_id=? AND status='OPEN'",
                             (self._now(),row['order_id']))
                result=self._entry_result(self._entry_row(conn,row['order_id']))
            self._log(conn,row['order_id'],'CANCEL',0,0,'CANCELLED','Explicit cancel',status='CANCELLED')
            return self._finish(conn,result,operation_id)

    def snapshot(self):
        from backend import database
        result=dict(mode='independent_exits',positions=[],entries=[],exits=[],unresolved_orders=[],uncovered={'LONG':0.0,'SHORT':0.0},
                    pending=False,quantity_conflict=False,quantity_conflicts=[],exit_groups={})
        with database.get_db_conn() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mock_positions'").fetchone():
                return result
            for row in conn.execute('SELECT * FROM mock_positions WHERE config_id=? AND symbol=? AND quantity>0',
                                    (self.config_id,self.symbol)):
                item=dict(row)
                item.update(order_id='POS-'+row['episode_id'],side='BUY' if row['side']=='LONG' else 'SELL',
                            pos_side=row['side'],price=row['entry_price'],amount=row['quantity'],is_filled=1,
                            stop_loss=None,take_profit=None,execution_mode='independent_exits')
                result['positions'].append(item)
                result['uncovered'][row['side']]=row['quantity']
            quantities=dict(result['uncovered'])
            result['entries']=[self._entry_result(dict(row)) for row in conn.execute("SELECT * FROM mock_orders WHERE config_id=? AND symbol=? "
                "AND execution_mode='independent_exits' AND status='OPEN' AND is_filled=0 AND (expire_at IS NULL OR expire_at>?)",
                (self.config_id,self.symbol,time.time()))]
            for raw_row in conn.execute("SELECT * FROM mock_exit_orders WHERE config_id=? AND symbol=? AND status IN ('OPEN','WAITING_TRIGGER')",
                                    (self.config_id,self.symbol)):
                row=dict(raw_row)
                item=self._result(row)
                available=quantities[row['pos_side']]
                item['coverage_estimate']=available if row['quantity_mode']=='all' else min(row['remaining'],available)
                item['coverage_is_estimate']=True
                item['execution_guaranteed']=False
                result['exits'].append(item)
                group=exit_group(row)
                observation=result['exit_groups'].setdefault(row['pos_side'],{}).setdefault(group,
                    dict(fixed_remaining=0.0,all_count=0,coverage_estimate=0.0,position_amount=available,quantity_conflict=False))
                if row['quantity_mode']=='all':
                    observation['all_count']+=1
                else:
                    observation['fixed_remaining']+=row['remaining']
                observation['coverage_estimate']=available if observation['all_count'] else min(observation['fixed_remaining'],available)
            for side,groups in result['exit_groups'].items():
                for group,observation in groups.items():
                    excess=max(observation['fixed_remaining']-quantities[side],0)
                    observation['quantity_conflict']=excess>1e-12
                    observation['excess_amount']=excess
                    if excess>1e-12:
                        result['quantity_conflicts'].append(dict(side=side,group=group,**observation))
                    if group=='stop_loss':
                        result['uncovered'][side]=max(quantities[side]-observation['coverage_estimate'],0)
            result['quantity_conflict']=bool(result['quantity_conflicts'])
            result['available_balance']=self._available(conn)
        return result

    def monitor(self, high, low, candle_ts_ms):
        from backend import database
        from backend.utils.independent_exits import positive
        positive(high,'candle high'); positive(low,'candle low')
        if low>high:
            raise ValueError('Candle low exceeds high')
        result=dict(filled=0,closed=0)
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Existing exits have priority over additions on an ambiguous OHLC bar.
            # In particular a downward crossing must honor an existing SL before
            # filling a lower buy limit and retroactively changing its cost basis.
            result['closed'] = self._monitor_exits(conn,high,low,candle_ts_ms)
            entries=conn.execute("SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND execution_mode='independent_exits' "
                                 "AND status='OPEN' AND is_filled=0 AND (expire_at IS NULL OR expire_at>?) ORDER BY timestamp,order_id",
                                 (self.config_id,self.symbol,time.time())).fetchall()
            for row in entries:
                created=(row['activated_at'] or database.TZ_CN.localize(datetime.strptime(row['timestamp'],'%Y-%m-%d %H:%M:%S')).timestamp())*1000
                if candle_ts_ms<=created:
                    continue
                if not (low<=row['price'] if row['side']=='BUY' else high>=row['price']):
                    continue
                side='LONG' if row['side']=='BUY' else 'SHORT'
                position=self._position(conn,side)
                if not position or position['episode_id']!=row['episode_id'] or position['status']=='DONE':
                    conn.execute("UPDATE mock_orders SET status='CANCELLED' WHERE order_id=?",(row['order_id'],))
                    continue
                executed=max(row['amount']-row['filled_amount'],0)
                quantity=position['quantity']+executed
                mean=(position['entry_price']*position['quantity']+row['price']*executed)/quantity
                conn.execute("UPDATE mock_orders SET status='FILLED',is_filled=1,filled_amount=amount WHERE order_id=?",(row['order_id'],))
                conn.execute("UPDATE mock_positions SET quantity=?,entry_price=?,opened_amount=opened_amount+?,status='ACTIVE',updated_at=? WHERE config_id=? AND symbol=? AND side=?",
                             (quantity,mean,executed,self._now(),self.config_id,self.symbol,side))
                self._log(conn,'fill:'+uuid.uuid4().hex,'ENTRY_'+row['side'],executed,row['price'],
                          'ENTRY_FILLED','Independent entry filled',parent=row['order_id'],status='FILLED')
                position.update(quantity=quantity,entry_price=mean,status='ACTIVE',opened_amount=position.get('opened_amount',0)+executed)
                self._history(conn,position)
                result['filled']+=1
            conn.commit()
        return result

    def _monitor_exits(self,conn,high,low,candle_ts_ms):
        exits=[dict(row) for row in conn.execute("SELECT * FROM mock_exit_orders WHERE config_id=? AND symbol=? AND status IN ('OPEN','WAITING_TRIGGER')",
                                                (self.config_id,self.symbol))]
        # OHLC cannot prove intrabar ordering. Check stop losses first; for each
        # direction check the nearest reachable trigger before farther levels.
        exits.sort(key=lambda r:(0 if exit_group(r)=='stop_loss' else 1,
            (-(r['trigger_price'] or 0) if r['pos_side']=='LONG' else (r['trigger_price'] or 0))
            if exit_group(r)=='stop_loss' else ((r['trigger_price'] or r['price'] or 0) if r['pos_side']=='LONG' else -(r['trigger_price'] or r['price'] or 0)),r['order_id']))
        closed=0
        for row in exits:
            if candle_ts_ms<=row['created_at']*1000:
                continue
            long=row['pos_side']=='LONG'
            conditional=row['order_type'] in CONDITIONAL_TYPES
            if conditional and not row['triggered_at']:
                rising=long if exit_group(row)=='take_profit' else not long
                touched=high>=row['trigger_price'] if rising else low<=row['trigger_price']
                if not touched:
                    continue
                child='SE-'+uuid.uuid4().hex[:16]
                conn.execute("UPDATE mock_exit_orders SET status='OPEN',triggered_at=?,parent_algo_id=order_id,execution_order_id=? WHERE order_id=? AND status IN ('OPEN','WAITING_TRIGGER')",
                             (candle_ts_ms,child,row['order_id']))
                row=self._exit_row(conn,row['order_id'])
                # A bar cannot locate the high/low relative to its trigger. Limit
                # children begin matching on a later bar, never retroactively.
                if row['order_type'] in LIMIT_TYPES:
                    continue
                closed+=int(self._fill_exit(conn,row,row['trigger_price']))
                continue
            if row['order_type'] in LIMIT_TYPES:
                if row['triggered_at'] and candle_ts_ms<=row['triggered_at']:
                    continue
                touched=high>=row['price'] if long else low<=row['price']
                fill_price=row['price']
            else:
                continue
            if touched:
                closed+=int(self._fill_exit(conn,row,fill_price))
        return closed
