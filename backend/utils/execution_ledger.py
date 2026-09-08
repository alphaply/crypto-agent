"""Account-scoped perpetual fills. Unknown ownership and missing PnL stay unknown.

REST reconciliation uses bounded time windows, recursively splitting full pages;
it never advances its checkpoint after a partial/failed query. Same-millisecond
overflow is reported incomplete instead of silently dropping fills.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from datetime import datetime, timedelta

from backend.database import get_db_conn, TZ_CN

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def account_scope(exchange, config_id: str) -> str:
    key = str(getattr(exchange, 'apiKey', '') or config_id)
    return hashlib.sha256(f'{exchange.id}:{key}'.encode()).hexdigest()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def register_order(
    scope: str,
    symbol: str,
    order_id,
    config_id: str,
    role: str,
    *,
    refresh: bool = True,
    **metadata,
):
    if not order_id:
        return
    with get_db_conn() as conn:
        existing = conn.execute('SELECT config_id,payload FROM execution_order_links WHERE account_scope=? AND symbol=? AND order_id=?',
                                (scope, symbol, str(order_id))).fetchone()
        if existing and existing['config_id'] != config_id:
            raise ValueError('Order ownership conflict')
        if existing:
            metadata = {**json.loads(existing['payload']), **{k: v for k, v in metadata.items() if v is not None}}
        conn.execute('INSERT INTO execution_order_links VALUES(?,?,?,?,?,?) ON CONFLICT(account_scope,symbol,order_id) '
                     'DO UPDATE SET role=excluded.role,payload=excluded.payload',
                     (scope, symbol, str(order_id), config_id, role, json.dumps(metadata, default=str)))
        # Events may arrive before the order acknowledgement; reconcile ownership later.
        conn.execute('UPDATE execution_fills SET config_id=? WHERE account_scope=? AND symbol=? AND order_id=?',
                     (config_id, scope, symbol, str(order_id)))
        conn.commit()
    if refresh:
        rebuild_execution_position_history(config_id, scope, symbol)


class ExecutionLedger:
    def __init__(self, exchange, config_id: str, symbol: str):
        self.exchange = exchange
        self.config_id = config_id
        exchange.load_markets()
        market = exchange.market(symbol)
        if not market.get('contract'):
            market = exchange.market(f'{symbol}:USDT')
        if not market.get('swap') or not market.get('linear'):
            raise ValueError('Execution ledger requires linear perpetuals; spot uses its own fill accounting')
        self.symbol = market['symbol']
        self.scope = account_scope(exchange, config_id)
        with _guard:
            self.lock = _locks.setdefault(f'{self.scope}:{self.symbol}', threading.Lock())
        self.index_saved_plans()

    def index_saved_plans(self):
        """Recover ownership after restart without assigning arbitrary account trades."""
        with get_db_conn() as conn:
            rows = conn.execute(
                'SELECT config_id,payload FROM real_protection_plans WHERE symbol=?',
                (self.symbol,),
            ).fetchall()
            event_rows = conn.execute(
                'SELECT config_id,payload FROM real_protection_events WHERE symbol=? ORDER BY id',
                (self.symbol,),
            ).fetchall()
            legacy_rows = conn.execute(
                "SELECT order_id,config_id,side,entry_price,take_profit,stop_loss,reason,event_type "
                "FROM orders WHERE config_id=? AND trade_mode='REAL' "
                "AND (symbol=? OR symbol=?) ORDER BY id",
                (self.config_id, self.symbol, self.symbol.split(':')[0]),
            ).fetchall()
        indexed = False
        for row in [*event_rows, *rows]:
            try:
                plan = json.loads(row['payload'])
            except (TypeError, json.JSONDecodeError):
                continue
            scope = plan.get('account_scope')
            if scope and scope != self.scope:
                continue
            if not scope and row['config_id'] != self.config_id:
                continue
            common = {
                'side': plan.get('side'),
                'episode_id': plan.get('episode_id'),
                'stop_loss': plan.get('stop_loss'),
                'take_profit': plan.get('take_profit'),
            }
            for item in plan.get('entries', []):
                register_order(
                    self.scope,
                    self.symbol,
                    item.get('id'),
                    row['config_id'],
                    'entry',
                    refresh=False,
                    planned_entry=item.get('price'),
                    **common,
                )
                indexed = indexed or bool(item.get('id'))
            for item in plan.get('legs', []):
                order_id = item.get('execution_order_id') or item.get('id')
                register_order(
                    self.scope,
                    self.symbol,
                    order_id,
                    row['config_id'],
                    'stop_loss' if item.get('kind') == 'sl' else 'take_profit',
                    refresh=False,
                    trigger_price=item.get('trigger_price'),
                    **common,
                )
                indexed = indexed or bool(order_id)
                if item.get('execution_order_id') and item.get('id') and item['execution_order_id'] != item['id']:
                    register_order(
                        self.scope,
                        self.symbol,
                        item.get('id'),
                        row['config_id'],
                        'stop_loss' if item.get('kind') == 'sl' else 'take_profit',
                        refresh=False,
                        trigger_price=item.get('trigger_price'),
                        **common,
                    )
            if plan.get('exit_order'):
                order_id = plan['exit_order'].get('id')
                register_order(
                    self.scope,
                    self.symbol,
                    order_id,
                    row['config_id'],
                    plan.get('exit_reason', 'emergency_exit'),
                    refresh=False,
                    **common,
                )
                indexed = indexed or bool(order_id)

        # Older versions recorded agent entry/exit IDs in orders but had no execution links.
        # This migration only claims orders already scoped to this exact configuration.
        for row in legacy_rows:
            raw_side = str(row['side'] or '').upper()
            event_type = str(row['event_type'] or '').upper()
            if raw_side.startswith('CLOSE_'):
                role = 'agent_exit'
                side = raw_side.removeprefix('CLOSE_')
            elif event_type == 'CLOSE_ORDER_CREATED':
                role = 'agent_exit'
                side = 'LONG' if 'LONG' in raw_side else ('SHORT' if 'SHORT' in raw_side else None)
            elif raw_side in {'BUY', 'SELL'} and event_type == 'ORDER_CREATED':
                role = 'entry'
                side = 'LONG' if raw_side == 'BUY' else 'SHORT'
            else:
                continue
            if side not in {'LONG', 'SHORT'}:
                continue
            register_order(
                self.scope,
                self.symbol,
                row['order_id'],
                row['config_id'],
                role,
                refresh=False,
                side=side,
                planned_entry=row['entry_price'],
                take_profit=row['take_profit'],
                stop_loss=row['stop_loss'],
                reason=row['reason'],
            )
            indexed = indexed or bool(row['order_id'])

        if indexed:
            rebuild_execution_position_history(self.config_id, self.scope, self.symbol)

    def status(self):
        with get_db_conn() as conn:
            row = conn.execute('SELECT payload FROM execution_sync WHERE account_scope=? AND symbol=?',
                               (self.scope, self.symbol)).fetchone()
        return json.loads(row['payload']) if row else {}

    def _status(self, value):
        with get_db_conn() as conn:
            conn.execute('INSERT INTO execution_sync VALUES(?,?,?) ON CONFLICT(account_scope,symbol) '
                         'DO UPDATE SET payload=excluded.payload', (self.scope, self.symbol, json.dumps(value)))
            conn.commit()

    def ingest(self, trades, refresh=True):
        owners = set()
        with get_db_conn() as conn:
            for trade in trades:
                if trade.get('id') is None or trade.get('timestamp') is None:
                    raise ValueError('Fill missing stable ID or timestamp')
                order_id = str(trade.get('order') or '')
                info = trade.get('info') or {}
                payload = {k: trade.get(k) for k in ('id', 'timestamp', 'side', 'price', 'amount', 'cost', 'fee', 'fees', 'takerOrMaker')}
                size = float(self.exchange.market(self.symbol).get('contractSize') or 1)
                amount = number(trade.get('amount'))
                payload['base_amount'] = amount * size if amount is not None else None
                payload['position_side'] = info.get('positionSide', info.get('posSide', info.get('ps')))
                payload['realized_pnl'] = number(trade.get('realizedPnl', info.get('realizedPnl', info.get('fillPnl', info.get('rp')))))
                previous = conn.execute('SELECT payload FROM execution_fills WHERE account_scope=? AND symbol=? AND trade_id=?',
                                        (self.scope, self.symbol, str(trade['id']))).fetchone()
                if previous:
                    old = json.loads(previous['payload'])
                    payload = {k: value if value is not None else old.get(k) for k, value in payload.items()}
                owner = conn.execute('SELECT config_id FROM execution_order_links WHERE account_scope=? AND symbol=? AND order_id=?',
                                     (self.scope, self.symbol, order_id)).fetchone()
                if owner:
                    owners.add(owner['config_id'])
                conn.execute('INSERT INTO execution_fills VALUES(?,?,?,?,?,?,?) '
                             'ON CONFLICT(account_scope,symbol,trade_id) DO UPDATE SET '
                             'config_id=COALESCE(excluded.config_id,execution_fills.config_id),payload=excluded.payload',
                             (self.scope, self.symbol, str(trade['id']), order_id, owner['config_id'] if owner else None,
                              int(trade['timestamp']), json.dumps(payload, default=str)))
            conn.commit()
        if refresh:
            for owner_config_id in owners:
                rebuild_execution_position_history(owner_config_id, self.scope, self.symbol)

    def sync(self, now_ms=None, start_ms=None, min_interval=30, max_calls=100):
        actual_now = int(time.time() * 1000)
        now_ms = min(int(now_ms), actual_now) if now_ms is not None else actual_now
        with self.lock:
            state = self.status()
            if start_ms is None and now_ms - state.get('attempted_at', 0) < min_interval * 1000:
                return state
            # Short memory reports seven days, so the first reconciliation must cover
            # the same interval. Later runs resume from the durable checkpoint.
            initial = now_ms - 7 * 24 * 3600 * 1000
            if start_ms is not None:
                start = int(start_ms)
            else:
                covered = merge_intervals(state.get('covered_intervals', []))
                previous_through = int(state.get('through_ms', initial))
                history_covered = any(
                    left <= initial and right >= previous_through
                    for left, right in covered
                )
                # Upgrade old 24-hour checkpoints by filling the missing six days once.
                start = previous_through - 60000 if history_covered else initial
            start = max(0, min(start, now_ms))
            # Save attempts even on failure; avoid retrying every five-second protection tick.
            state.update(attempted_at=now_ms, complete=False, error=None)
            self._status(state)
            calls = 0
            limit = 1000 if self.exchange.id in {'binance', 'binanceusdm'} else 100

            def fetch_window(left, right):
                nonlocal calls
                calls += 1
                if calls > max_calls:
                    raise RuntimeError('Reconciliation page budget reached; checkpoint retained')
                rows = self.exchange.fetch_my_trades(self.symbol, since=left, limit=limit,
                                                      params={'until': right})
                rows = rows or []
                selected = [r for r in rows if left <= int(r.get('timestamp') or 0) <= right]
                self.ingest(selected, refresh=False)
                if len(rows) >= limit:
                    if left == right:
                        raise RuntimeError('Too many fills within one millisecond; completeness unknown')
                    middle = (left + right) // 2
                    fetch_window(left, middle)
                    fetch_window(middle + 1, right)
                elif any(int(r.get('timestamp') or 0) < left or int(r.get('timestamp') or 0) > right for r in rows):
                    raise RuntimeError('Exchange did not honor requested time window')

            try:
                # Binance limits an individual time range; daily chunks also bound recovery work.
                cursor = start
                while cursor <= now_ms:
                    end = min(now_ms, cursor + 24 * 3600 * 1000 - 1)
                    fetch_window(cursor, end)
                    # Retain completed chunks across long outages even if a later chunk
                    # hits the call budget. Never include the failed chunk in coverage.
                    intervals = merge_intervals(state.get('covered_intervals', []) + [[cursor, end]])
                    state.update(from_ms=intervals[-1][0], through_ms=intervals[-1][1], covered_intervals=intervals)
                    self._status(state)
                    cursor = end + 1
                intervals = merge_intervals(state.get('covered_intervals', []) + [[start, now_ms]])
                state.update(from_ms=intervals[-1][0], through_ms=intervals[-1][1], covered_intervals=intervals,
                             complete=True, error=None, calls=calls)
            except Exception as exc:
                state.update(error=str(exc), calls=calls)
            rebuild_execution_position_history(self.config_id, self.scope, self.symbol)
            self._status(state)
            return state

    def sync_income(self, start_ms, end_ms, max_pages=50):
        """Account/symbol cash flows for reconciliation, never attributed by symbol alone."""
        if self.exchange.id not in {'binance', 'binanceusdm'}:
            with self.lock:
                result = {'complete': False, 'error': 'Income adapter not available for this exchange'}
                state = self.status()
                state['income_sync'] = result
                self._status(state)
            return result
        with self.lock:
            result = {'from_ms': start_ms, 'through_ms': end_ms, 'complete': False}
            try:
                market_id = self.exchange.market(self.symbol)['id']
                for page in range(1, max_pages + 1):
                    rows = self.exchange.fapiPrivateGetIncome({'symbol': market_id, 'startTime': start_ms,
                                                              'endTime': end_ms, 'limit': 1000, 'page': page})
                    with get_db_conn() as conn:
                        for row in rows:
                            if not start_ms <= int(row['time']) <= end_ms:
                                raise ValueError('Income response outside requested range')
                            conn.execute('INSERT INTO execution_income VALUES(?,?,?,?,?,?) '
                                         'ON CONFLICT(account_scope,income_type,transaction_id) DO UPDATE SET payload=excluded.payload',
                                         (self.scope, self.symbol, row['incomeType'], str(row['tranId']), int(row['time']), json.dumps(row)))
                        conn.commit()
                    if len(rows) < 1000:
                        result['complete'] = True
                        break
                if not result['complete']:
                    result['error'] = 'Income pagination budget reached'
            except Exception as exc:
                result['error'] = str(exc)
            state = self.status()
            state['income_sync'] = result
            self._status(state)
            return result


def execution_context(config_id: str, hours=24, limit=20, now=None, scope=None, symbol=None):
    """Bounded facts separate from model memory; all-time active plans remain visible."""
    now = now or datetime.now(TZ_CN)
    if now.tzinfo is None:
        now = TZ_CN.localize(now)
    end_ms = int(now.timestamp() * 1000)
    start_ms = max(0, end_ms - int(hours * 3600 * 1000))
    with get_db_conn() as conn:
        conditions = 'f.config_id=? AND f.timestamp_ms>=? AND f.timestamp_ms<?'
        args = [config_id, start_ms, end_ms]
        if scope:
            conditions += ' AND f.account_scope=?'
            args.append(scope)
        if symbol:
            conditions += ' AND f.symbol=?'
            args.append(symbol)
        rows = conn.execute('SELECT f.*,l.role,l.payload AS link_metadata FROM execution_fills f '
                            'LEFT JOIN execution_order_links l ON f.account_scope=l.account_scope '
                            'AND f.symbol=l.symbol AND f.order_id=l.order_id WHERE ' + conditions +
                            ' ORDER BY f.timestamp_ms DESC', args).fetchall()
        plans = [json.loads(r['payload']) for r in conn.execute(
            'SELECT payload FROM real_protection_plans WHERE config_id=?', (config_id,)).fetchall()]
        scopes = {r['account_scope'] for r in rows} | {p.get('account_scope') for p in plans}
        if scope:
            scopes = {scope}
        sync = []
        unknown_count = 0
        income_rows = []
        for key in scopes - {None}:
            for row in conn.execute('SELECT symbol,payload FROM execution_sync WHERE account_scope=?', (key,)):
                if symbol and row['symbol'] != symbol:
                    continue
                sync.append({'symbol': row['symbol'], **json.loads(row['payload'])})
            unknown_count += conn.execute('SELECT COUNT(*) FROM execution_fills WHERE account_scope=? '
                                         'AND config_id IS NULL AND timestamp_ms>=? AND timestamp_ms<? '
                                         'AND (? IS NULL OR symbol=?)', (key, start_ms, end_ms, symbol, symbol)).fetchone()[0]
            income_rows.extend(conn.execute('SELECT payload FROM execution_income WHERE account_scope=? '
                                            'AND timestamp_ms>=? AND timestamp_ms<? AND (? IS NULL OR symbol=?)',
                                            (key, start_ms, end_ms, symbol, symbol)).fetchall())
    facts = []
    fees = {}
    pnl = 0
    missing_pnl = 0
    missing_fee = 0
    for row in rows:
        fill = json.loads(row['payload'])
        value = fill.get('realized_pnl')
        if value is None:
            missing_pnl += 1
        else:
            pnl += value
        items = fill.get('fees') or ([fill['fee']] if fill.get('fee') else [])
        if not items:
            missing_fee += 1
        for fee in items:
            cost = number(fee.get('cost'))
            currency = fee.get('currency')
            if cost is None or not currency:
                missing_fee += 1
            else:
                fees[currency] = fees.get(currency, 0) + cost
        if len(facts) < limit:
            facts.append({**fill, 'order_id': row['order_id'], 'symbol': row['symbol'],
                          'role': row['role'] or 'unknown', 'evidence': json.loads(row['link_metadata'] or '{}')})
    active = [{k: p.get(k) for k in ('symbol', 'side', 'state', 'stop_loss', 'take_profit', 'error', 'verified_at')}
              for p in plans if p['state'] != 'DONE' and (not scope or p.get('account_scope') == scope)
              and (not symbol or p['symbol'] == symbol)]
    income_totals = {}
    for row in income_rows:
        item = json.loads(row['payload'])
        key = f"{item['incomeType']}:{item['asset']}"
        value = number(item.get('income'))
        if value is not None:
            income_totals[key] = income_totals.get(key, 0) + value
    window_complete = bool(sync) and all(any(left <= start_ms and right >= end_ms - 1
                                             for left, right in state.get('covered_intervals', [])) for state in sync)
    payload = {'window_hours': hours, 'from_ms': start_ms, 'through_ms': end_ms, 'sync': sync,
               'window_complete': window_complete, 'unknown_owner_fill_count': unknown_count,
               'account_income_by_type_asset_not_agent_pnl': income_totals,
               'fill_count': len(rows), 'omitted_details': max(0, len(rows) - limit), 'recent_fills': facts,
               'known_realized_pnl_before_fees': pnl, 'missing_pnl_count': missing_pnl,
               'fees_by_currency': fees, 'missing_fee_count': missing_fee, 'active_protection': active}
    from backend.utils.execution_metrics import metrics_context
    payload['execution_review'] = metrics_context(config_id, start_ms, end_ms, scope, symbol)
    payload['position_cycles'] = position_cycles(config_id, start_ms, end_ms, scope, symbol)
    return ('## 近期合约执行事实（直接读取账本，优先于记忆）\n'
            '仅已归属本配置的成交；未知归属不计入。成交条数不是完整交易笔数；费用不可重复扣除。'
            'account_income是账户/品种资金流水，可能含其他策略或手工操作，不与本Agent成交盈亏相加。'
            'sync缺失/失败或覆盖不足时不能声称历史完整。role表示订单用途，成交才证明执行；缺失数字为未知。\n'
            + json.dumps(payload, ensure_ascii=False, default=str))


def _reconstruct_position_cycles(config_id, start_ms, end_ms, scope=None, symbol=None):
    """Reconstruct every owned entry-to-flat cycle without inventing missing fills."""
    with get_db_conn() as conn:
        rows = conn.execute('SELECT f.*,l.role,l.payload AS link_metadata FROM execution_fills f '
                            'JOIN execution_order_links l ON f.account_scope=l.account_scope AND f.symbol=l.symbol '
                            'AND f.order_id=l.order_id WHERE f.config_id=? AND f.timestamp_ms<? '
                            'AND (? IS NULL OR f.account_scope=?) AND (? IS NULL OR f.symbol=?) '
                            'ORDER BY f.timestamp_ms, LENGTH(f.trade_id), f.trade_id',
                            (config_id, end_ms, scope, scope, symbol, symbol)).fetchall()
    current = {}
    completed = []
    unmatched = 0
    for row in rows:
        fill = json.loads(row['payload'])
        metadata = json.loads(row['link_metadata'])
        side = metadata.get('side')
        if side not in {'LONG', 'SHORT'}:
            unmatched += 1
            continue
        key = (row['account_scope'], row['symbol'], side)
        quantity = number(fill.get('base_amount', fill.get('amount')))
        price = number(fill.get('price'))
        if not quantity or not price:
            unmatched += 1
            continue
        cycle = current.get(key)
        if row['role'] == 'entry':
            if not cycle:
                cycle = {
                    'account_scope': row['account_scope'],
                    'config_id': config_id,
                    'symbol': row['symbol'],
                    'side': side,
                    'episode_id': metadata.get('episode_id'),
                    'opened_at_ms': row['timestamp_ms'],
                    'remaining_base': 0,
                    'entered_base': 0,
                    'exited_base': 0,
                    'entry_cost': 0,
                    'exit_cost': 0,
                    'realized_pnl_before_fees': 0,
                    'missing_pnl': False,
                    'missing_fees': False,
                    'fees_by_currency': {},
                    'entry_trade_ids': [],
                    'exit_trade_ids': [],
                    'entry_order_ids': [],
                    'exit_order_ids': [],
                    'exit_reasons': [],
                    'take_profit': number(metadata.get('take_profit')),
                    'stop_loss': number(metadata.get('stop_loss')),
                }
                current[key] = cycle
            cycle['episode_id'] = cycle.get('episode_id') or metadata.get('episode_id')
            cycle['take_profit'] = number(metadata.get('take_profit')) or cycle.get('take_profit')
            cycle['stop_loss'] = number(metadata.get('stop_loss')) or cycle.get('stop_loss')
            cycle['remaining_base'] += quantity
            cycle['entered_base'] += quantity
            cycle['entry_cost'] += quantity * price
            cycle['entry_trade_ids'].append(row['trade_id'])
            if row['order_id'] and row['order_id'] not in cycle['entry_order_ids']:
                cycle['entry_order_ids'].append(row['order_id'])
        else:
            if not cycle or quantity > cycle['remaining_base'] + 1e-10:
                unmatched += 1
                current.pop(key, None)
                continue
            cycle['remaining_base'] = max(0, cycle['remaining_base'] - quantity)
            cycle['exited_base'] += quantity
            cycle['exit_cost'] += quantity * price
            if row['role'] not in cycle['exit_reasons']:
                cycle['exit_reasons'].append(row['role'])
            cycle['exit_trade_ids'].append(row['trade_id'])
            if row['order_id'] and row['order_id'] not in cycle['exit_order_ids']:
                cycle['exit_order_ids'].append(row['order_id'])
            if row['role'] == 'take_profit':
                cycle['take_profit'] = number(metadata.get('trigger_price')) or cycle.get('take_profit')
            elif row['role'] == 'stop_loss':
                cycle['stop_loss'] = number(metadata.get('trigger_price')) or cycle.get('stop_loss')
        pnl = fill.get('realized_pnl')
        if row['role'] != 'entry' and pnl is None:
            cycle['missing_pnl'] = True
        elif pnl is not None:
            cycle['realized_pnl_before_fees'] += pnl
        fee_items = fill.get('fees') or ([fill['fee']] if fill.get('fee') else [])
        if not fee_items:
            cycle['missing_fees'] = True
        for fee in fee_items:
            cost = number(fee.get('cost'))
            currency = fee.get('currency')
            if cost is None or not currency:
                cycle['missing_fees'] = True
            else:
                cycle['fees_by_currency'][currency] = cycle['fees_by_currency'].get(currency, 0) + cost
        if cycle['remaining_base'] <= 1e-10:
            cycle['closed_at_ms'] = row['timestamp_ms']
            cycle['entry_vwap'] = cycle['entry_cost'] / cycle['entered_base']
            cycle['exit_vwap'] = cycle['exit_cost'] / cycle['exited_base']
            cycle['holding_seconds'] = (cycle['closed_at_ms'] - cycle['opened_at_ms']) / 1000
            if not cycle.get('episode_id'):
                identity = (
                    f"{row['account_scope']}:{row['symbol']}:{side}:"
                    f"{cycle['entry_trade_ids'][0]}"
                )
                cycle['episode_id'] = hashlib.sha256(identity.encode()).hexdigest()
            if cycle['closed_at_ms'] >= start_ms:
                completed.append(cycle)
            current.pop(key, None)
    return {
        'completed': completed,
        'unmatched_fill_count': unmatched,
        'open_cycles': list(current.values()),
    }


def rebuild_execution_position_history(config_id: str, scope: str, symbol: str):
    """Materialize closed trading cycles as the canonical local history table."""
    with get_db_conn() as conn:
        newest = conn.execute(
            'SELECT MAX(timestamp_ms) FROM execution_fills '
            'WHERE account_scope=? AND symbol=? AND config_id=?',
            (scope, symbol, config_id),
        ).fetchone()[0]
    end_ms = int(newest or 0) + 1
    rebuilt = _reconstruct_position_cycles(config_id, 0, end_ms, scope, symbol)
    updated_at_ms = int(time.time() * 1000)
    with get_db_conn() as conn:
        conn.execute(
            'DELETE FROM execution_position_history '
            'WHERE account_scope=? AND config_id=? AND symbol=?',
            (scope, config_id, symbol),
        )
        for cycle in rebuilt['completed']:
            realized_pnl = None if cycle['missing_pnl'] else cycle['realized_pnl_before_fees']
            exit_reason = ','.join(cycle['exit_reasons'])
            conn.execute(
                '''INSERT INTO execution_position_history(
                       position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                       entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,
                       fees_json,exit_reason,updated_at_ms,payload
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    cycle['episode_id'],
                    scope,
                    config_id,
                    symbol,
                    cycle['side'],
                    cycle['opened_at_ms'],
                    cycle['closed_at_ms'],
                    cycle['entry_vwap'],
                    cycle['exit_vwap'],
                    cycle['entered_base'],
                    cycle.get('take_profit'),
                    cycle.get('stop_loss'),
                    realized_pnl,
                    json.dumps(cycle['fees_by_currency'], ensure_ascii=False),
                    exit_reason,
                    updated_at_ms,
                    json.dumps(cycle, ensure_ascii=False, default=str),
                ),
            )
        conn.commit()
    return rebuilt


def position_cycles(config_id, start_ms, end_ms, scope=None, symbol=None):
    """Return bounded cycle facts for prompts while preserving complete totals."""
    rebuilt = _reconstruct_position_cycles(config_id, start_ms, end_ms, scope, symbol)
    completed = rebuilt['completed']
    return {
        'completed_count': len(completed),
        'completed': completed[-10:],
        'omitted_completed': max(0, len(completed) - 10),
        'unmatched_fill_count': rebuilt['unmatched_fill_count'],
        'open_cycles': rebuilt['open_cycles'],
        'limits': '仅可重建已归属且开平仓成交链完整的周期；未匹配成交不构造虚假开仓。实际持仓以交易所快照为准；周期盈亏已包含在成交总计，不重复相加。',
    }
