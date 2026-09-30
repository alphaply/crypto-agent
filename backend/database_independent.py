"""Transactional aggregate-cost simulated positions and independent exit orders."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime


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
        price REAL, trigger_price REAL, amount REAL NOT NULL, remaining REAL NOT NULL,
        status TEXT NOT NULL, reason TEXT, timestamp TEXT NOT NULL,
        created_at REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS mock_trade_operations (
        config_id TEXT NOT NULL, symbol TEXT NOT NULL, operation_id TEXT NOT NULL,
        result TEXT NOT NULL, PRIMARY KEY(config_id,symbol,operation_id))''')
    columns = {row[1] for row in cursor.execute('PRAGMA table_info(mock_orders)')}
    for name in ('execution_mode', 'episode_id'):
        if name not in columns:
            cursor.execute(f'ALTER TABLE mock_orders ADD COLUMN {name} TEXT')
    if 'activated_at' not in columns:
        cursor.execute('ALTER TABLE mock_orders ADD COLUMN activated_at REAL')


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

    def _replay(self, conn, operation_id):
        if not operation_id:
            return None
        row = conn.execute('SELECT result FROM mock_trade_operations WHERE config_id=? AND symbol=? AND operation_id=?',
                           (self.config_id, self.symbol, operation_id)).fetchone()
        return json.loads(row['result']) if row else None

    def _finish(self, conn, result, operation_id=None):
        if operation_id:
            conn.execute('INSERT INTO mock_trade_operations VALUES(?,?,?,?)',
                         (self.config_id, self.symbol, operation_id, json.dumps(result)))
        conn.commit()
        return result

    def _available(self, conn):
        account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (self.config_id,)).fetchone()
        balance = float(account['balance']) if account else 10000.0
        pending = conn.execute("SELECT COALESCE(SUM(price*amount),0) FROM mock_orders WHERE config_id=? AND status='OPEN' "
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

    def open(self, op, operation_id=None):
        from backend import database
        from backend.utils.independent_exits import positive
        if op.stop_loss is not None or op.take_profit is not None:
            raise ValueError('Independent entries do not accept attached TP/SL')
        amount, price = positive(op.amount, 'amount'), positive(op.entry_price, 'entry price')
        side = 'LONG' if op.action == 'BUY_LIMIT' else 'SHORT'
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay = self._replay(conn, operation_id)
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
                price,amount,expire_at,status,is_filled,execution_mode,episode_id,activated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'OPEN',0,'independent_exits',?,?)''',
                (oid, self._now(), self.symbol, self.config_id, self.config_id,
                 'BUY' if side == 'LONG' else 'SELL', price, amount, expiry, episode,time.time()))
            self._log(conn, oid, 'BUY' if side == 'LONG' else 'SELL', amount, price, 'ORDER_CREATED', op.reason)
            return self._finish(conn, dict(id=oid, status='open', amount=amount, remaining=amount,
                                           price=price, episode_id=episode, pending=False), operation_id)

    def _exit_row(self, conn, order_id):
        row = conn.execute('SELECT * FROM mock_exit_orders WHERE order_id=? AND config_id=? AND symbol=?',
                           (order_id, self.config_id, self.symbol)).fetchone()
        if not row:
            raise ValueError('Exit order is not owned by this configuration')
        return dict(row)

    def _result(self, row):
        return {**row, 'id': row['order_id'], 'status': row['status'].lower(), 'pending': False}

    def close(self, pos_side, amount, exit_type, current_price, price=None, trigger_price=None,
              reason='', operation_id=None):
        from backend import database
        from backend.utils.independent_exits import positive, validate_exit
        amount = positive(amount, 'amount', zero=True)
        validate_exit(exit_type, pos_side, current_price, price, trigger_price)
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay = self._replay(conn, operation_id)
            if replay is not None:
                return replay
            position = self._position(conn, pos_side)
            if not position or position['quantity'] <= 0:
                raise ValueError('Exit orders require an already filled position')
            quantity = amount or position['quantity']
            if quantity > position['quantity'] + 1e-12:
                raise ValueError('Exit quantity exceeds position')
            outstanding = conn.execute("SELECT COALESCE(SUM(remaining),0) FROM mock_exit_orders WHERE config_id=? AND symbol=? "
                "AND episode_id=? AND exit_type=? AND status='OPEN'",
                (self.config_id, self.symbol, position['episode_id'], exit_type)).fetchone()[0]
            if exit_type != 'market' and outstanding + quantity > position['quantity'] + 1e-12:
                raise ValueError('Outstanding exits of this type exceed position')
            oid = 'SX-' + uuid.uuid4().hex[:16]
            conn.execute('''INSERT INTO mock_exit_orders(order_id,config_id,symbol,pos_side,episode_id,exit_type,
                price,trigger_price,amount,remaining,status,reason,timestamp,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?)''',
                (oid,self.config_id,self.symbol,pos_side,position['episode_id'],exit_type,price,trigger_price,
                 quantity,quantity,reason,self._now(),time.time()))
            self._log(conn, oid, 'CLOSE_' + pos_side, quantity, price or trigger_price or current_price,
                      'CLOSE_ORDER_CREATED', reason, parent=position['episode_id'])
            if exit_type == 'market':
                self._fill_exit(conn, self._exit_row(conn, oid), current_price)
            return self._finish(conn, self._result(self._exit_row(conn, oid)), operation_id)

    def _resize(self, conn, position):
        for kind in ('take_profit_limit', 'stop_market'):
            rows = conn.execute("SELECT * FROM mock_exit_orders WHERE config_id=? AND symbol=? AND episode_id=? "
                                "AND exit_type=? AND status='OPEN'",
                                (self.config_id, self.symbol, position['episode_id'], kind)).fetchall()
            total = sum(row['remaining'] for row in rows)
            if total <= position['quantity'] + 1e-12:
                continue
            ratio = position['quantity'] / total
            for row in rows:
                remaining = row['remaining'] * ratio
                conn.execute('UPDATE mock_exit_orders SET remaining=?,amount=?,status=? WHERE order_id=?',
                             (remaining, row['amount']-row['remaining']+remaining,
                              'OPEN' if remaining > 1e-12 else 'CANCELLED', row['order_id']))
                self._log(conn, 'resize:' + uuid.uuid4().hex, 'CLOSE_' + position['side'], remaining,
                          row['price'] or row['trigger_price'], 'EXIT_RESIZED',
                          'Position reduced; proportional quantity correction', parent=row['order_id'])

    def _fill_exit(self, conn, row, fill_price):
        # The same write transaction checks the current order/position and updates
        # balances, quantity and events; repeated monitor passes cannot realize twice.
        live = self._exit_row(conn, row['order_id'])
        if live['status'] != 'OPEN':
            return False
        position = self._position(conn, live['pos_side'])
        if not position or position['episode_id'] != live['episode_id'] or position['quantity'] <= 0:
            conn.execute("UPDATE mock_exit_orders SET status='CANCELLED',remaining=0 WHERE order_id=?", (live['order_id'],))
            return False
        quantity = min(live['remaining'], position['quantity'])
        pnl = (fill_price-position['entry_price']) * quantity * (1 if position['side'] == 'LONG' else -1)
        remaining = max(position['quantity']-quantity, 0)
        state = 'DONE' if remaining <= 1e-12 else 'ACTIVE'
        if state == 'DONE':
            remaining = 0
        conn.execute('UPDATE mock_positions SET quantity=?,realized_pnl=realized_pnl+?,status=?,updated_at=? '
                     'WHERE config_id=? AND symbol=? AND side=?',
                     (remaining,pnl,state,self._now(),self.config_id,self.symbol,position['side']))
        conn.execute("UPDATE mock_exit_orders SET status='CLOSED',remaining=0,realized_pnl=? WHERE order_id=?", (pnl,live['order_id']))
        conn.execute('UPDATE mock_accounts SET balance=balance+? WHERE config_id=?', (pnl,self.config_id))
        # Preserve existing insolvency reset behavior without a separate connection.
        conn.execute('UPDATE mock_accounts SET balance=10000,failures=failures+1 WHERE config_id=? AND balance<1000', (self.config_id,))
        account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (self.config_id,)).fetchone()
        conn.execute('INSERT INTO mock_balance_history(config_id,symbol,timestamp,balance,unrealized_pnl,total_equity) VALUES(?,?,?,?,0,?)',
                     (self.config_id,self.symbol,self._now(),account['balance'],account['balance']))
        self._log(conn, 'fill:' + uuid.uuid4().hex, 'CLOSE_' + position['side'], quantity,fill_price,
                  'MANUAL_CLOSE' if live['exit_type']=='market' else 'SL_HIT' if live['exit_type']=='stop_market' else 'TP_HIT',
                  live['reason'],pnl,parent=live['order_id'],status='CLOSED')
        position.update(quantity=remaining,realized_pnl=position['realized_pnl']+pnl,status=state)
        self._resize(conn, position)
        if state == 'DONE':
            entries = conn.execute("SELECT order_id FROM mock_orders WHERE config_id=? AND symbol=? AND episode_id=? AND status='OPEN'",
                                   (self.config_id,self.symbol,position['episode_id'])).fetchall()
            conn.execute("UPDATE mock_orders SET status='CANCELLED',close_time=? WHERE config_id=? AND symbol=? AND episode_id=? AND status='OPEN'",
                         (self._now(),self.config_id,self.symbol,position['episode_id']))
            for entry in entries:
                self._log(conn,entry['order_id'],'CANCEL',0,0,'CANCELLED','Position cycle ended',status='CANCELLED')
        self._history(conn,position,fill_price)
        return True

    def amend_entry(self, order_id, entry_price=None, amount=None, reason='', operation_id=None, pos_side=None):
        from backend import database
        from backend.utils.independent_exits import positive
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay=self._replay(conn,operation_id)
            if replay is not None:
                return replay
            row=conn.execute("SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=? "
                             "AND execution_mode='independent_exits' AND status='OPEN' AND is_filled=0",
                             (order_id,self.config_id,self.symbol)).fetchone()
            if not row:
                raise ValueError('No owned pending entry to amend')
            if pos_side is not None and pos_side != ('LONG' if row['side']=='BUY' else 'SHORT'):
                raise ValueError('Entry amendment cannot change position direction')
            new_price=positive(entry_price,'entry price') if entry_price is not None else row['price']
            new_amount=positive(amount,'amount') if amount is not None else row['amount']
            if new_price*new_amount > self._available(conn)+row['price']*row['amount']+1e-9:
                raise ValueError('Amended entry exceeds available simulated funds')
            conn.execute('UPDATE mock_orders SET price=?,amount=?,activated_at=? WHERE order_id=?',
                         (new_price,new_amount,time.time(),order_id))
            self._log(conn,'amend:'+uuid.uuid4().hex,row['side'],new_amount,new_price,'ORDER_AMENDED',reason,parent=order_id)
            return self._finish(conn,dict(id=order_id,status='open',price=new_price,amount=new_amount,
                                         remaining=new_amount,pending=False),operation_id)

    def amend_exit(self, order_id, amount=None, price=None, trigger_price=None, reason='',
                   operation_id=None, current_price=None):
        from backend import database
        from backend.utils.independent_exits import positive,validate_exit
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay=self._replay(conn,operation_id)
            if replay is not None:
                return replay
            row=self._exit_row(conn,order_id)
            if row['status']!='OPEN' or row['exit_type']=='market':
                raise ValueError('Only open conditional exits may be amended')
            position=self._position(conn,row['pos_side'])
            if current_price is None:
                raise ValueError('A fresh current_price is required to validate an exit amendment')
            new_price=row['price'] if price is None else positive(price,'price')
            new_trigger=row['trigger_price'] if trigger_price is None else positive(trigger_price,'trigger price')
            validate_exit(row['exit_type'],row['pos_side'],current_price,new_price,new_trigger)
            new_amount=row['remaining'] if amount is None else positive(amount,'amount')
            other=conn.execute("SELECT COALESCE(SUM(remaining),0) FROM mock_exit_orders WHERE config_id=? AND symbol=? "
                               "AND episode_id=? AND exit_type=? AND status='OPEN' AND order_id!=?",
                               (self.config_id,self.symbol,row['episode_id'],row['exit_type'],order_id)).fetchone()[0]
            if other+new_amount>position['quantity']+1e-12:
                raise ValueError('Amended exit quantities exceed position')
            conn.execute('UPDATE mock_exit_orders SET price=?,trigger_price=?,amount=?,remaining=?,reason=?,created_at=?,timestamp=? WHERE order_id=?',
                         (new_price,new_trigger,new_amount,new_amount,reason,time.time(),self._now(),order_id))
            self._log(conn,'amend:'+uuid.uuid4().hex,'CLOSE_'+row['pos_side'],new_amount,new_price or new_trigger,
                      'ORDER_AMENDED',reason,parent=order_id)
            return self._finish(conn,self._result(self._exit_row(conn,order_id)),operation_id)

    def cancel(self, order_id, operation_id=None):
        from backend import database
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            replay=self._replay(conn,operation_id)
            if replay is not None:
                return replay
            row=conn.execute('SELECT * FROM mock_exit_orders WHERE order_id=? AND config_id=? AND symbol=?',
                             (order_id,self.config_id,self.symbol)).fetchone()
            if row:
                if row['status']=='OPEN':
                    conn.execute("UPDATE mock_exit_orders SET status='CANCELLED' WHERE order_id=?",(order_id,))
                else:
                    return self._finish(conn,self._result(dict(row)),operation_id)
            else:
                row=conn.execute("SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=? "
                                 "AND execution_mode='independent_exits'",
                                 (order_id,self.config_id,self.symbol)).fetchone()
                if not row or row['is_filled']:
                    raise ValueError('No owned pending order to cancel')
                conn.execute("UPDATE mock_orders SET status='CANCELLED',close_time=? WHERE order_id=? AND status='OPEN'",
                             (self._now(),order_id))
            self._log(conn,order_id,'CANCEL',0,0,'CANCELLED','Explicit cancel',status='CANCELLED')
            return self._finish(conn,dict(id=order_id,status='cancelled',pending=False),operation_id)

    def snapshot(self):
        from backend import database
        result=dict(mode='independent_exits',positions=[],entries=[],exits=[],uncovered={'LONG':0.0,'SHORT':0.0},pending=False)
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
            result['entries']=[dict(row) for row in conn.execute("SELECT * FROM mock_orders WHERE config_id=? AND symbol=? "
                "AND execution_mode='independent_exits' AND status='OPEN' AND is_filled=0 AND (expire_at IS NULL OR expire_at>?)",
                (self.config_id,self.symbol,time.time()))]
            for row in conn.execute("SELECT * FROM mock_exit_orders WHERE config_id=? AND symbol=? AND status='OPEN'",
                                    (self.config_id,self.symbol)):
                result['exits'].append(self._result(dict(row)))
                if row['exit_type']=='stop_market':
                    result['uncovered'][row['pos_side']]=max(result['uncovered'][row['pos_side']]-row['remaining'],0)
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
                quantity=position['quantity']+row['amount']
                mean=(position['entry_price']*position['quantity']+row['price']*row['amount'])/quantity
                conn.execute("UPDATE mock_orders SET status='FILLED',is_filled=1 WHERE order_id=?",(row['order_id'],))
                conn.execute("UPDATE mock_positions SET quantity=?,entry_price=?,opened_amount=opened_amount+?,status='ACTIVE',updated_at=? WHERE config_id=? AND symbol=? AND side=?",
                             (quantity,mean,row['amount'],self._now(),self.config_id,self.symbol,side))
                self._log(conn,'fill:'+uuid.uuid4().hex,'ENTRY_'+row['side'],row['amount'],row['price'],
                          'ENTRY_FILLED','Independent entry filled',parent=row['order_id'],status='FILLED')
                position.update(quantity=quantity,entry_price=mean,status='ACTIVE',opened_amount=position.get('opened_amount',0)+row['amount'])
                self._history(conn,position)
                result['filled']+=1
            conn.commit()
        return result

    def _monitor_exits(self,conn,high,low,candle_ts_ms):
        exits=[dict(row) for row in conn.execute("SELECT * FROM mock_exit_orders WHERE config_id=? AND symbol=? AND status='OPEN'",
                                                (self.config_id,self.symbol))]
        # OHLC cannot prove intrabar ordering. Check stop losses first; for each
        # direction check the nearest reachable trigger before farther levels.
        exits.sort(key=lambda r:(0 if r['exit_type']=='stop_market' else 1,
            (-(r['trigger_price'] or 0) if r['pos_side']=='LONG' else (r['trigger_price'] or 0))
            if r['exit_type']=='stop_market' else ((r['price'] or 0) if r['pos_side']=='LONG' else -(r['price'] or 0)),r['order_id']))
        closed=0
        for row in exits:
            if candle_ts_ms<=row['created_at']*1000:
                continue
            long=row['pos_side']=='LONG'
            if row['exit_type']=='stop_market':
                touched=low<=row['trigger_price'] if long else high>=row['trigger_price']
                fill_price=row['trigger_price']
            elif row['exit_type']=='take_profit_limit':
                touched=high>=row['price'] if long else low<=row['price']
                fill_price=row['price']
            else:
                continue
            if touched:
                closed+=int(self._fill_exit(conn,row,fill_price))
        return closed
