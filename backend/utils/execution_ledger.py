"""Account-scoped perpetual fills. Unknown ownership and missing PnL stay unknown.

REST reconciliation uses bounded time windows, recursively splitting full pages;
it never advances its checkpoint after a partial/failed query. Same-millisecond
overflow is reported incomplete instead of silently dropping fills.
"""
from __future__ import annotations
from backend.storage import lock_scope, Error as StorageError

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
    legacy = hashlib.sha256(f'{exchange.id}:{key}'.encode()).hexdigest()
    with get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
        if 'management_execution_scopes' in tables:
            kind = getattr(exchange, 'options', {}).get('defaultType') or ('spot' if getattr(exchange, 'spot', False) else 'swap')
            canonical = conn.execute('SELECT s.legacy_scope FROM management_account_aliases a '
                                     'JOIN management_shared_accounts s ON s.physical_account=a.physical_account '
                                     'WHERE a.legacy_scope=%s AND s.market_type=%s', (legacy, kind)).fetchone()
            if canonical:
                return canonical[0]
    return legacy


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
        lock_scope(conn, 'execution-source', scope, symbol)
        existing = conn.execute('SELECT config_id,role,payload FROM execution_order_links WHERE account_scope=%s AND symbol=%s AND order_id=%s',
                                (scope, symbol, str(order_id))).fetchone()
        from backend.utils.execution_history_work import historical_publication
        if existing and historical_publication.get() and (
                json.loads(existing['payload']).get('exchange_channel') or existing['role'] == 'entry'):
            return  # Historical snapshots cannot overwrite a current authoritative link.
        if existing and existing['config_id'] != config_id:
            tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}
            granted = False
            if 'visible_management_claims' in tables:
                granted = conn.execute("SELECT 1 FROM visible_management_claims c "
                                       "JOIN management_shared_participants p ON p.claim_id=c.claim_id "
                                       "JOIN management_account_aliases a ON a.physical_account=c.physical_account "
                                       "WHERE p.actor=%s AND a.legacy_scope=%s AND c.market_type='swap' "
                                       "AND c.resource=%s AND c.state='owned' LIMIT 1",
                                       (config_id, scope, symbol)).fetchone()
                granted = granted or conn.execute("SELECT 1 FROM visible_management_claims c JOIN management_resource_links r ON r.claim_id=c.claim_id "
                                       "WHERE c.owner=%s AND c.state='owned' AND r.identity=%s AND r.original_owner=%s LIMIT 1",
                                       (config_id, str(order_id), existing['config_id'])).fetchone()
                if not granted:
                    granted = conn.execute("SELECT 1 FROM real_protection_plans WHERE config_id=%s AND symbol=%s "
                                           "AND (payload::jsonb->>'original_owner')=%s LIMIT 1",
                                           (config_id, symbol, existing['config_id'])).fetchone()
            if not granted:
                raise ValueError('Order ownership conflict')
            # Current management may change. Original fill provenance never does.
            return
        if existing:
            prior = json.loads(existing['payload'])
            metadata = {**prior, **{k: v for k, v in metadata.items() if v is not None}}
            if prior == metadata and existing['role'] == role:
                return
        conn.execute('INSERT INTO execution_order_links(account_scope,symbol,order_id,config_id,role,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_scope,symbol,order_id) DO UPDATE SET role=excluded.role,payload=excluded.payload',
                     (scope, symbol, str(order_id), config_id, role, json.dumps(metadata, default=str)))
        # Events may arrive before the order acknowledgement; reconcile ownership later.
        conn.execute('UPDATE execution_fills SET config_id=%s WHERE account_scope=%s AND symbol=%s AND order_id=%s AND config_id IS DISTINCT FROM %s',
                     (config_id, scope, symbol, str(order_id), config_id))
        from backend.utils.execution_history_work import request_history
        request_history(config_id, scope, symbol, conn=conn)
        conn.commit()


class ExecutionLedger:
    def __init__(self, exchange, config_id: str, symbol: str):
        self.exchange = exchange
        self.config_id = config_id
        if not getattr(exchange, 'markets', None):
            from backend.utils.public_catalogue import load_public_catalogue
            load_public_catalogue(exchange, market_type='swap')
        market = exchange.market(symbol)
        if not market.get('contract'):
            market = exchange.market(f'{symbol}:USDT')
        if not market.get('swap') or not market.get('linear'):
            raise ValueError('Execution ledger requires linear perpetuals; spot uses its own fill accounting')
        self.symbol = market['symbol']
        self.scope = account_scope(exchange, config_id)
        with _guard:
            self.lock = _locks.setdefault(f'{self.scope}:{self.symbol}', threading.Lock())
        from backend.utils.execution_plan_index import index_status
        self.index_state = index_status(self)

    def _retire_legacy_algo_aliases(self, plans, legacy_rows, *, conditional_ids=None, regular_ids=None):
        """Quarantine old bare algo IDs without deleting any execution evidence.

        Trades report regular order IDs. Before channel-qualified identities,
        a persisted algo parent could occupy that namespace and claim unrelated
        fills. History and current plans are both needed: a later child or a
        genuine regular order can prove that the same numeric ID is also valid.
        """
        from backend.utils.exit_orders import is_conditional, parse_order_reference
        conditional_ids = set() if conditional_ids is None else conditional_ids
        regular_ids = set() if regular_ids is None else regular_ids

        def identity(owner, value):
            return (owner, parse_order_reference(value)[1]) if value is not None else None

        for owner, plan in plans:
            for entry in plan.get('entries', []):
                if entry.get('id'):
                    regular_ids.add(identity(owner, entry['id']))
            if (plan.get('exit_order') or {}).get('id'):
                regular_ids.add(identity(owner, plan['exit_order']['id']))
            independent = plan.get('execution_mode') == 'independent_exits'
            for record in [*plan.get('legs', []), *plan.get('exits', [])]:
                if record.get('id'):
                    destination = conditional_ids if independent and is_conditional(record) else regular_ids
                    destination.add(identity(owner, record['id']))
                if record.get('execution_order_id'):
                    regular_ids.add(identity(owner, record['execution_order_id']))
        for row in legacy_rows:
            if str(row['side'] or '').upper() in {'BUY', 'SELL'} and str(row['event_type'] or '').upper() == 'ORDER_CREATED':
                regular_ids.add(identity(row['config_id'], row['order_id']))

        affected = set()
        with get_db_conn() as conn:
            lock_scope(conn, 'execution-source', self.scope, self.symbol)
            if hasattr(regular_ids, 'connection'):
                regular_ids.connection = conn
            for owner, raw_id in conditional_ids:
                link = conn.execute('SELECT storage_id AS rowid,config_id,role FROM execution_order_links WHERE account_scope=%s AND symbol=%s AND order_id=%s',
                                    (self.scope, self.symbol, raw_id)).fetchone()
                if not link or link['config_id'] != owner:
                    continue
                from backend.utils.sqlite_blob import bounded_text
                from backend.utils.runtime_capacity import positive_setting, CapacityBusy
                maximum = 4 * 1024 * 1024
                try:
                    with bounded_text(conn, 'execution_order_links', 'payload', link['rowid'], limit=maximum) as raw:
                        metadata = json.loads(raw)
                except CapacityBusy:
                    raise
                except (TypeError, ValueError):
                    metadata = {}
                # An entry, an explicitly indexed regular order, or an actual
                # child is positive evidence; the algo collision cannot erase it.
                if (owner, raw_id) in regular_ids or link['role'] == 'entry' or metadata.get('exchange_channel') == 'regular':
                    regular_ids.add((owner, raw_id))
                    continue
                canonical = 'algo:' + raw_id
                parent = conn.execute('SELECT storage_id AS rowid,config_id FROM execution_order_links WHERE account_scope=%s AND symbol=%s AND order_id=%s',
                                      (self.scope, self.symbol, canonical)).fetchone()
                if parent and parent['config_id'] != owner:
                    raise ValueError('Order ownership conflict')
                parent_metadata = {}
                if parent:
                    with bounded_text(conn, 'execution_order_links', 'payload', parent['rowid'], limit=maximum) as raw:
                        parent_metadata = json.loads(raw)
                parent_metadata.update(exchange_channel='algo', legacy_alias_retired=raw_id,
                                       legacy_alias_role=link['role'], legacy_alias_metadata=metadata)
                conn.execute('INSERT INTO execution_order_links(account_scope,symbol,order_id,config_id,role,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_scope,symbol,order_id) DO UPDATE SET payload=excluded.payload',
                             (self.scope, self.symbol, canonical, owner, link['role'], json.dumps(parent_metadata)))
                conn.execute('DELETE FROM execution_order_links WHERE account_scope=%s AND symbol=%s AND order_id=%s AND config_id=%s',
                             (self.scope, self.symbol, raw_id, owner))
                # Keep trade IDs, timestamps, order IDs and raw fill payloads. Only
                # the unproven attribution is removed, including previous ingests.
                conn.execute('UPDATE execution_fills SET config_id=NULL WHERE account_scope=%s AND symbol=%s AND order_id=%s AND config_id=%s',
                             (self.scope, self.symbol, raw_id, owner))
                affected.add(owner)
            conn.commit()
        if hasattr(regular_ids, 'connection'):
            regular_ids.connection = None
        return conditional_ids, regular_ids, affected

    def index_saved_plans(self):
        """Advance durable ownership migration without materializing history."""
        from backend.utils.execution_plan_index import advance_index
        self.index_state = advance_index(self)
        return self.index_state

    def _index_plan(self, owner, plan):
        indexed = False
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
                owner,
                'entry',
                refresh=False,
                planned_entry=item.get('price'),
                exchange_channel='regular',
                **{**common, 'episode_id': item.get('origin_episode_id', plan.get('episode_id'))},
            )
            indexed |= bool(item.get('id'))

        for item in [*plan.get('legs', []), *plan.get('exits', [])]:
            from backend.utils.exit_orders import is_conditional, exit_group, order_reference
            independent = plan.get('execution_mode') == 'independent_exits'
            # Algo IDs must not claim regular fills with the same numeric ID.
            parent_id = order_reference(item, current=False) if independent and is_conditional(item) else item.get('id')
            order_id = item.get('execution_order_id') or parent_id
            group = exit_group(item) if independent else None
            role = ('stop_loss' if group == 'stop_loss' or item.get('kind') == 'sl' else
                    'agent_exit' if group in {'limit', 'market'} else 'take_profit')
            register_order(
                self.scope,
                self.symbol,
                order_id,
                owner,
                role,
                refresh=False,
                trigger_price=item.get('trigger_price'),
                exchange_channel=('algo' if str(order_id).startswith('algo:') else 'regular') if independent else None,
                **{**common, 'episode_id': item.get('origin_episode_id', plan.get('episode_id'))},
            )
            indexed |= bool(order_id)

            if item.get('execution_order_id') and parent_id and item['execution_order_id'] != parent_id:
                register_order(
                    self.scope,
                    self.symbol,
                    parent_id,
                    owner,
                    role,
                    refresh=False,
                    trigger_price=item.get('trigger_price'),
                    exchange_channel=('algo' if str(parent_id).startswith('algo:') else 'regular') if independent else None,
                    **{**common, 'episode_id': item.get('origin_episode_id', plan.get('episode_id'))},
                )
        if plan.get('exit_order'):
            order_id = plan['exit_order'].get('id')
            register_order(
                self.scope,
                self.symbol,
                order_id,
                owner,
                plan.get('exit_reason', 'emergency_exit'),
                refresh=False,
                exchange_channel='regular',
                **common,
            )
            indexed |= bool(order_id)
        return indexed


    def _index_legacy_row(self, row, conditional_ids, regular_ids):
        from backend.utils.exit_orders import parse_order_reference
        identity = (row['config_id'], parse_order_reference(row['order_id'])[1])
        if identity in conditional_ids and identity not in regular_ids:
            # An old CLOSE_ORDER_CREATED event cannot disambiguate an algo
            # parent from an identically numbered regular execution.
            return
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
            return
        if side not in {'LONG', 'SHORT'}:
            return
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
            exchange_channel='regular',
        )
        return bool(row['order_id'])


    def status(self):
        with get_db_conn() as conn:
            row = conn.execute('SELECT payload FROM execution_sync WHERE account_scope=%s AND symbol=%s',
                               (self.scope, self.symbol)).fetchone()
        return json.loads(row['payload']) if row else {}

    def _status(self, value):
        with get_db_conn() as conn:
            conn.execute('INSERT INTO execution_sync(account_scope,symbol,payload) VALUES(%s,%s,%s) ON CONFLICT(account_scope,symbol) DO UPDATE SET payload=excluded.payload', (self.scope, self.symbol, json.dumps(value)))
            conn.commit()

    def ingest(self, trades, refresh=True):
        owners = set()
        with get_db_conn() as conn:
            lock_scope(conn, 'execution-source', self.scope, self.symbol)
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
                previous = conn.execute('SELECT payload FROM execution_fills WHERE account_scope=%s AND symbol=%s AND trade_id=%s',
                                        (self.scope, self.symbol, str(trade['id']))).fetchone()
                if previous:
                    old = json.loads(previous['payload'])
                    payload = {k: value if value is not None else old.get(k) for k, value in payload.items()}
                owner = conn.execute('SELECT config_id FROM execution_order_links WHERE account_scope=%s AND symbol=%s AND order_id=%s',
                                     (self.scope, self.symbol, order_id)).fetchone()
                if not (self.index_state.get('complete') or self.index_state.get('ownership_ready')):
                    # Raw fills remain durable, but a partially published
                    # historical ownership migration cannot assign new facts.
                    owner = None
                if owner:
                    owners.add(owner['config_id'])
                conn.execute('INSERT INTO execution_fills(account_scope,symbol,trade_id,order_id,config_id,timestamp_ms,payload) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_scope,symbol,trade_id) DO UPDATE SET config_id=COALESCE(excluded.config_id,execution_fills.config_id),payload=excluded.payload',
                             (self.scope, self.symbol, str(trade['id']), order_id, owner['config_id'] if owner else None,
                              int(trade['timestamp']), json.dumps(payload, default=str)))
            conn.commit()
        if refresh:
            # An external close changes an owned cycle even without an order link.
            with get_db_conn() as conn:
                owners.update(r[0] for r in conn.execute(
                    'SELECT DISTINCT config_id FROM execution_order_links WHERE account_scope=%s AND symbol=%s',
                    (self.scope, self.symbol)))
            from backend.utils.execution_history_work import request_history
            for owner_config_id in owners:
                request_history(owner_config_id, self.scope, self.symbol)

    def sync(self, now_ms=None, start_ms=None, min_interval=30, *, require_index=True):
        if require_index and not (self.index_state.get('complete') or self.index_state.get('ownership_ready')):
            return {**self.status(), 'complete': False, 'ownership_index': self.index_state,
                    'error': 'Ownership evidence indexing incomplete; exchange synchronization deferred'}
        actual_now = int(time.time() * 1000)
        now_ms = min(int(now_ms), actual_now) if now_ms is not None else actual_now
        with self.lock:
            state = self.status()
            if start_ms is None and now_ms - state.get('attempted_at', 0) < min_interval * 1000:
                return state
            initial = now_ms - 7 * 24 * 3600 * 1000
            covered = merge_intervals(state.get('covered_intervals', []))
            previous_through = int(state.get('through_ms', initial))
            if start_ms is not None:
                start = int(start_ms)
            else:
                history_covered = any(left <= initial and right >= previous_through
                                      for left, right in covered)
                start = previous_through - 60000 if history_covered else initial
            start = max(0, min(start, now_ms))
            limit = 1000 if self.exchange.id in {'binance', 'binanceusdm'} else 100
            day = 24 * 3600 * 1000
            calls = 0

            def gaps(left, right, intervals):
                cursor = left
                result = []
                for low, high in merge_intervals(intervals):
                    if high < cursor or low > right:
                        continue
                    if low > cursor:
                        result.append([cursor, min(right, low - 1)])
                    cursor = max(cursor, high + 1)
                    if cursor > right:
                        break
                if cursor <= right:
                    result.append([cursor, right])
                return result

            def chunks(left, right):
                result = []
                while left <= right:
                    end = min(right, left + day - 1)
                    result.append([left, end])
                    left = end + 1
                return result

            # Save the split frontier, not just daily roots. An exhausted budget
            # can then resume after completed leaves, even after process restart.
            pending = [[max(start, int(left)), min(now_ms, int(right))]
                       for left, right in state.get('pending_windows', [])
                       if int(right) >= start and int(left) <= now_ms]
            jobs = list(pending)
            for left, right in gaps(start, now_ms, covered + jobs):
                jobs.extend(chunks(left, right))

            scans = [list(window) for window in state.get('scan_windows', [])]
            if not pending and covered:
                # Late REST/stream facts can appear in a previously covered
                # interval. Reuse the previous sparse partitions so refreshing
                # dense recent history does not rediscover the whole split tree.
                low, high = max(start, previous_through - 60000), min(now_ms, previous_through)
                if low <= high:
                    refreshed = []
                    for left, right, _count in scans:
                        left, right = max(low, int(left)), min(high, int(right))
                        if left <= right:
                            refreshed.append([left, right])
                    for left, right in gaps(low, high, refreshed):
                        refreshed.extend(chunks(left, right))
                    for left, right in refreshed:
                        jobs.extend(gaps(left, right, jobs))
            jobs.sort()
            state.update(attempted_at=now_ms, complete=False, error=None,
                         pending_windows=jobs, pending_from_ms=start, pending_through_ms=now_ms)
            self._status(state)

            def record_leaf(left, right, count):
                nonlocal scans, covered
                # A fresh leaf replaces its previous partitions. Counts on old
                # clipped pieces remain conservative until they are refreshed.
                revised = []
                for old_left, old_right, old_count in scans:
                    if old_right < left or old_left > right:
                        revised.append([old_left, old_right, old_count])
                    else:
                        if old_left < left:
                            revised.append([old_left, left - 1, old_count])
                        if old_right > right:
                            revised.append([right + 1, old_right, old_count])
                revised.append([left, right, count])
                scans = []
                for window in sorted(revised):
                    if (scans and scans[-1][1] + 1 == window[0]
                            and scans[-1][2] + window[2] < limit
                            and window[1] - scans[-1][0] < day):
                        scans[-1][1] = window[1]
                        scans[-1][2] += window[2]
                    else:
                        scans.append(window)
                covered = merge_intervals(covered + [[left, right]])
                state.update(from_ms=covered[-1][0], through_ms=covered[-1][1],
                             covered_intervals=covered, scan_windows=scans)

            try:
                while jobs:
                    left, right = jobs[0]
                    calls += 1
                    rows = self.exchange.fetch_my_trades(self.symbol, since=left, limit=limit,
                                                         params={'until': right}) or []
                    if any(int(row.get('timestamp') or 0) < left
                           or int(row.get('timestamp') or 0) > right for row in rows):
                        raise RuntimeError('Exchange did not honor requested time window')
                    self.ingest(rows, refresh=False)
                    if len(rows) >= limit:
                        if left == right:
                            raise RuntimeError('Too many fills within one millisecond; completeness unknown')
                        middle = (left + right) // 2
                        jobs[:1] = [[left, middle], [middle + 1, right]]
                    else:
                        record_leaf(left, right, len(rows))
                        jobs.pop(0)
                    # Persist every completed leaf and split frontier. Ingest is
                    # idempotent if a crash happens between the two commits.
                    state.update(pending_windows=list(jobs), calls=calls)
                    self._status(state)
                state.update(complete=any(left <= start and right >= now_ms
                                          for left, right in covered), error=None, calls=calls)
            except Exception as exc:
                state.update(error=str(exc), calls=calls, pending_windows=list(jobs))
            from backend.utils.execution_history_work import request_history
            request_history(self.config_id, self.scope, self.symbol)
            self._status(state)
            return state

    def sync_income(self, start_ms, end_ms):
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
                page = 1
                seen_pages = set()
                while True:
                    rows = self.exchange.fapiPrivateGetIncome({'symbol': market_id, 'startTime': start_ms,
                                                              'endTime': end_ms, 'limit': 1000, 'page': page})
                    if rows:
                        signature = hashlib.sha256(json.dumps(sorted(
                            (row['incomeType'], str(row['tranId']), int(row['time']))
                            for row in rows)).encode()).hexdigest()
                        if signature in seen_pages:
                            raise ValueError('Income pagination did not advance; complete evidence unavailable')
                        seen_pages.add(signature)
                    with get_db_conn() as conn:
                        for row in rows:
                            if not start_ms <= int(row['time']) <= end_ms:
                                raise ValueError('Income response outside requested range')
                            conn.execute('INSERT INTO execution_income(account_scope,symbol,income_type,transaction_id,timestamp_ms,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_scope,income_type,transaction_id) DO UPDATE SET payload=excluded.payload',
                                         (self.scope, self.symbol, row['incomeType'], str(row['tranId']), int(row['time']), json.dumps(row)))
                        conn.commit()
                    if len(rows) < 1000:
                        result['complete'] = True
                        break
                    page += 1
            except Exception as exc:
                result['error'] = str(exc)
            state = self.status()
            state['income_sync'] = result
            self._status(state)
            return result


def execution_context(config_id: str, hours=24, limit=None, now=None, scope=None, symbol=None,
                      *, account_wide=False, include_cycles=True):
    """Complete facts in the requested time window; active plans remain visible."""
    if account_wide and (not scope or not symbol):
        raise ValueError('Account statistics require an account and symbol')
    now = now or datetime.now(TZ_CN)
    if now.tzinfo is None:
        now = TZ_CN.localize(now)
    end_ms = int(now.timestamp() * 1000)
    start_ms = max(0, end_ms - int(hours * 3600 * 1000))
    symbol_column = "split_part(symbol, ':', 1)" if account_wide and symbol and ':' not in symbol else 'symbol'
    with get_db_conn() as conn:
        conditions = ('f.account_scope=%s' if account_wide else 'f.config_id=%s')
        conditions += ' AND f.timestamp_ms>=%s AND f.timestamp_ms<%s'
        args = [scope if account_wide else config_id, start_ms, end_ms]
        if scope:
            conditions += ' AND f.account_scope=%s'
            args.append(scope)
        if symbol:
            conditions += ' AND ' + symbol_column.replace('symbol', 'f.symbol') + '=%s'
            args.append(symbol)
        rows = conn.execute('SELECT f.*,l.role,l.payload AS link_metadata FROM execution_fills f '
                            'LEFT JOIN execution_order_links l ON f.account_scope=l.account_scope '
                            'AND f.symbol=l.symbol AND f.order_id=l.order_id WHERE ' + conditions +
                            ' ORDER BY f.timestamp_ms DESC', args).fetchall()
        plans = [] if account_wide else [json.loads(r['payload']) for r in conn.execute(
            'SELECT payload FROM real_protection_plans WHERE config_id=%s', (config_id,)).fetchall()]
        scopes = {r['account_scope'] for r in rows} | {p.get('account_scope') for p in plans}
        if scope:
            scopes = {scope}
        sync = []
        index_complete = conn.execute(f"SELECT 1 FROM execution_plan_index_runs WHERE config_id=%s AND phase!='complete' AND (%s::text IS NULL OR account_scope=%s) AND (%s::text IS NULL OR {symbol_column}=%s) LIMIT 1",
                                      (config_id, scope, scope, symbol, symbol)).fetchone() is None
        unknown_count = 0
        income_rows = []
        for key in scopes - {None}:
            for row in conn.execute('SELECT symbol,payload FROM execution_sync WHERE account_scope=%s', (key,)):
                if symbol and (row['symbol'].split(':')[0] if account_wide and ':' not in symbol else row['symbol']) != symbol:
                    continue
                sync.append({'symbol': row['symbol'], **json.loads(row['payload'])})
            unknown_count += conn.execute(f'SELECT COUNT(*) FROM execution_fills WHERE account_scope=%s AND config_id IS NULL AND timestamp_ms>=%s AND timestamp_ms<%s AND (%s::text IS NULL OR {symbol_column}=%s)', (key, start_ms, end_ms, symbol, symbol)).fetchone()[0]
            income_rows.extend(conn.execute(f'SELECT payload FROM execution_income WHERE account_scope=%s AND timestamp_ms>=%s AND timestamp_ms<%s AND (%s::text IS NULL OR {symbol_column}=%s)',
                                            (key, start_ms, end_ms, symbol, symbol)).fetchall())
    facts = []
    fees = {}
    pnl = 0
    missing_pnl = 0
    missing_fee = 0
    for row in rows:
        fill = json.loads(row['payload'])
        value = number(fill.get('realized_pnl'))
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
        if limit is None or len(facts) < limit:
            facts.append({**fill, 'order_id': row['order_id'], 'symbol': row['symbol'],
                          'role': row['role'] or 'unknown', 'evidence': json.loads(row['link_metadata'] or '{}')})
    active = [{**{k: p.get(k) for k in ('symbol', 'side', 'state', 'stop_loss', 'take_profit', 'error', 'verified_at')},
               **({'position_source': 'manual_adoption', 'adoption': {
                   key: p['adoption'].get(key) for key in ('amount', 'entry_price', 'adopted_at', 'side')
               }} if p.get('adoption') else {})}
              for p in plans if p['state'] != 'DONE' and (not scope or p.get('account_scope') == scope)
              and (not symbol or p['symbol'] == symbol)]
    income_totals = {}
    for row in income_rows:
        item = json.loads(row['payload'])
        key = f"{item['incomeType']}:{item['asset']}"
        value = number(item.get('income'))
        if value is not None:
            income_totals[key] = income_totals.get(key, 0) + value
    window_complete = (account_wide or index_complete) and bool(sync) and all(any(left <= start_ms and right >= end_ms - 1
                                             for left, right in state.get('covered_intervals', [])) for state in sync)
    payload = {'window_hours': hours, 'from_ms': start_ms, 'through_ms': end_ms, 'sync': sync,
               'statistics_scope': 'account_symbol' if account_wide else 'agent_attributed_fills',
               'window_complete': window_complete, 'ownership_index_complete': index_complete,
               'unknown_owner_fill_count': unknown_count,
               'account_income_by_type_asset_not_agent_pnl': income_totals,
               'fill_count': len(rows), 'omitted_details': len(rows) - len(facts), 'recent_fills': facts,
               'known_realized_pnl_before_fees': pnl, 'missing_pnl_count': missing_pnl,
               'fees_by_currency': fees, 'missing_fee_count': missing_fee, 'active_protection': active}
    from backend.utils.execution_metrics import metrics_context
    if not account_wide:
        payload['execution_review'] = metrics_context(config_id, start_ms, end_ms, scope, symbol)
    if include_cycles:
        payload['position_cycles'] = position_cycles(config_id, start_ms, end_ms, scope, symbol,
                                                     account_wide=account_wide)
    ownership_note = ('当前交易所账户、当前标的全部已记录成交，包含手动与其他任务成交；不是单个Agent战绩。'
                      if account_wide else '仅已归属本配置的成交；未知归属不计入。')
    return ('## 近期合约执行事实（直接读取账本，优先于记忆）\n'
            + ownership_note + '成交条数不是完整交易笔数；费用不可重复扣除。'
            'account_income是账户/品种资金流水，可能含其他策略或手工操作，不与本Agent成交盈亏相加。'
            'sync缺失/失败或覆盖不足时不能声称历史完整。role表示订单用途，成交才证明执行；缺失数字为未知。\n'
            + json.dumps(payload, ensure_ascii=False, default=str))


def recent_activity_data(config_id: str, symbol: str, now=None, *, hours=168, include_cycles=True) -> dict:
    from backend.utils.trade_evidence import configured_account_scope
    if not symbol or '/' not in symbol:
        raise ValueError('A concrete symbol is required for real performance')
    scope = configured_account_scope(config_id)
    with get_db_conn() as conn:
        alias = conn.execute('SELECT s.legacy_scope FROM management_account_aliases a '
                             'JOIN management_shared_accounts s ON s.physical_account=a.physical_account '
                             "WHERE a.legacy_scope=%s AND s.market_type='swap'", (scope,)).fetchone()
        if alias:
            scope = alias[0]
    raw = execution_context(config_id, hours=hours, now=now, scope=scope, symbol=symbol,
                            account_wide=True, include_cycles=include_cycles)
    data = json.loads(raw[raw.index('\n{') + 1:])
    return data


def recent_activity_summary(config_id: str, symbol: str, now=None, *, data=None) -> str:
    """Compact realized performance; accounting diagnostics are on-demand only."""
    from backend.utils.performance_context import profit_metric
    data = recent_activity_data(config_id, symbol, now, include_cycles=False) if data is None else data
    currency = symbol.split(':')[-1] if ':' in symbol else symbol.split('/')[-1]
    metric = profit_metric(data, currency)
    value = f"{metric['amount']:+.2f} {currency}" if metric['available'] else '暂不可完整统计'
    coverage = '' if data.get('window_complete') else ' 同步尚未完整覆盖所选时段，此金额仅代表本地已记录成交。'
    return f"最近7天已实现盈亏（账户该标的已记录成交、扣同币种费用）：{value}。{coverage}"


def closed_positions_from_activity(data: dict) -> list[dict]:
    """Render account cycles from the same fill evidence as the profit total."""
    cycles = data.get('position_cycles') or {}
    if cycles.get('scan_complete') is not True:
        raise ValueError('Closed-position evidence is incomplete')
    rows = []
    for cycle in sorted(cycles['completed'], key=lambda item: item['closed_at_ms'], reverse=True):
        currency = cycle['settlement_currency']
        fees = cycle['fees_by_currency']
        gross = cycle['realized_pnl_before_fees']
        complete = not cycle['missing_fees'] and bool(fees) and set(fees) <= {currency}
        rows.append({
            'position_id': cycle['episode_id'], 'symbol': cycle['symbol'], 'side': cycle['side'],
            'opened_at': datetime.fromtimestamp(cycle['opened_at_ms'] / 1000, TZ_CN).strftime('%Y-%m-%d %H:%M:%S'),
            'closed_at': datetime.fromtimestamp(cycle['closed_at_ms'] / 1000, TZ_CN).strftime('%Y-%m-%d %H:%M:%S'),
            'entry_price': cycle['entry_vwap'], 'close_price': cycle['exit_vwap'],
            'amount': cycle['entered_base'], 'take_profit': cycle.get('take_profit'),
            'stop_loss': cycle.get('stop_loss'), 'realized_pnl': gross,
            'net_realized_pnl': gross - fees.get(currency, 0) if gross is not None and complete else None,
            'fees': fees, 'fees_complete': complete, 'settlement_currency': currency,
            'exit_reason': ','.join(cycle['exit_reasons']), 'add_count': cycle['add_count'],
            'exit_count': cycle['exit_count'], 'events': cycle['events'],
            'protection_history': cycle['protection_history'], 'cycle': cycle,
            'source': 'execution_fills', 'statistics_scope': 'account_symbol',
        })
    return rows


def _reconstruct_position_cycles(config_id, start_ms, end_ms, scope=None, symbol=None,
                                 *, cycle_sink=None, read_conn=None, account_wide=False):
    """Reconstruct ordered facts with a bounded work set; incomplete never means zero."""
    from backend.utils.execution_cycles import reconstruct_cycles
    from backend.utils.runtime_capacity import positive_setting,reserve_bytes
    maximum=4*1024*1024
    with reserve_bytes('parse',maximum*4):
        if read_conn is not None:
            return reconstruct_cycles(config_id,start_ms,end_ms,scope,symbol,
                                      cycle_sink=cycle_sink,read_conn=read_conn,account_wide=account_wide)
        with get_db_conn() as conn:
            conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
            return reconstruct_cycles(config_id,start_ms,end_ms,scope,symbol,
                                      cycle_sink=cycle_sink,read_conn=conn,account_wide=account_wide)


def rebuild_execution_position_history(config_id: str, scope: str, symbol: str):
    """Stage complete cycles on disk; publish only a full, unchanged source snapshot."""
    with get_db_conn() as conn:
        conn.execute('INSERT INTO execution_source_revisions(account_scope,symbol,revision) VALUES(%s,%s,0) '
                     'ON CONFLICT(account_scope,symbol) DO NOTHING', (scope,symbol))
        conn.execute('DROP TABLE IF EXISTS pg_temp.execution_history_stage')
        conn.execute('CREATE TEMP TABLE execution_history_stage AS '
                     'SELECT * FROM execution_position_history WHERE false')
        conn.commit()
        try:
            conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
            version=conn.execute('SELECT revision FROM execution_source_revisions WHERE account_scope=%s AND symbol=%s',
                                 (scope,symbol)).fetchone()[0]
            newest=conn.execute('SELECT MAX(timestamp_ms) FROM execution_fills '
                                'WHERE account_scope=%s AND symbol=%s',(scope,symbol)).fetchone()[0]
            end_ms=int(newest or 0)+1
            updated_at_ms=int(time.time()*1000)
            def stage(cycle):
                realized_pnl = None if cycle['missing_pnl'] else cycle['realized_pnl_before_fees']
                exit_reason = ','.join(cycle['exit_reasons'])
                conn.execute(
                    '''INSERT INTO execution_history_stage(
                           position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,
                           entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,
                           fees_json,exit_reason,updated_at_ms,payload
                       ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
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
            rebuilt=_reconstruct_position_cycles(config_id,0,end_ms,scope,symbol,
                                                  cycle_sink=stage,read_conn=conn)
            conn.commit()
            if not rebuilt['scan_complete']:
                return {**rebuilt,'published':False,'retained_previous':True}
            # The source revision row serializes publication with all source writers.
            # Changes in unrelated account/symbol scopes do not invalidate this view.
            lock_scope(conn, 'execution-source', scope, symbol)
            if conn.execute('SELECT revision FROM execution_source_revisions WHERE account_scope=%s AND symbol=%s FOR UPDATE', (scope,symbol)).fetchone()[0]!=version:
                conn.rollback()
                return {**rebuilt,'scan_complete':False,'published':False,
                        'retained_previous':True,'reason_code':'cycle_source_changed'}
            conn.execute('DELETE FROM execution_position_history '
                         'WHERE account_scope=%s AND config_id=%s AND symbol=%s',(scope,config_id,symbol))
            fields = 'position_id,account_scope,config_id,symbol,side,opened_at_ms,closed_at_ms,entry_price,close_price,amount,take_profit,stop_loss,realized_pnl,fees_json,exit_reason,updated_at_ms,payload'
            conn.execute(f'INSERT INTO execution_position_history({fields}) SELECT {fields} FROM execution_history_stage')
            conn.execute('DROP TABLE execution_history_stage')
            conn.commit()
            return {**rebuilt,'published':True,'retained_previous':False}
        finally:
            # Pooled sessions must not retain an abandoned staging table.
            try:
                if not conn.closed:
                    conn.rollback()
                    conn.execute('DROP TABLE IF EXISTS pg_temp.execution_history_stage')
                    conn.commit()
            except StorageError:
                # A broken session cannot retain temp tables after it closes.
                conn.close()



def position_cycles(config_id, start_ms, end_ms, scope=None, symbol=None, *, account_wide=False):
    """Return every reconstructed cycle in the selected time window."""
    rebuilt = _reconstruct_position_cycles(config_id, start_ms, end_ms, scope, symbol,account_wide=account_wide)
    completed = rebuilt['completed']
    return {
        'completed_count': rebuilt['completed_count'],
        'completed': completed,
        'omitted_completed': rebuilt['omitted_completed'],
        'unmatched_fill_count': rebuilt['unmatched_fill_count'],
        'excluded_cycle_count': rebuilt['excluded_cycle_count'],
        'open_cycles': rebuilt['open_cycles'],
        'scan_complete': rebuilt['scan_complete'],
        'projection_complete': rebuilt['projection_complete'],
        'reason_code': rebuilt['reason_code'],
        'next_cursor': rebuilt['next_cursor'],
        'limits': ('当前账户该标的全部成交按同方向数量归零划分，包含手动与其他任务成交；不是单个Agent战绩。'
                   if account_wide else '按账户同方向成交归零划分；外部退出仅注明未知原因。混合归属入场不计入本策略战绩。')
                  + '实际持仓以交易所快照为准；周期盈亏与窗口内成交盈亏口径不同，不能相加。',
    }
