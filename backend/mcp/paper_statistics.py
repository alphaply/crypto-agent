"""Shared Paper statistics, derived only from durable account and event evidence.

Writers update projections inside their existing account transaction. Historical
accounts are rebuilt in bounded background pages; readers never scan raw ledgers.
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Literal

from backend.storage import record, try_lock_scope
from . import store

Period = Literal['current_cycle', '7d', '30d', 'all']
PERIODS = ('current_cycle', '7d', '30d', 'all')
ZERO = Decimal(0)
CURRENT_FIELDS = ('cash', 'available_cash', 'reserved_cash', 'debt', 'net_equity',
                  'used_margin', 'unrealized_pnl', 'performance_pnl')


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _number(value):
    return None if value is None else float(Decimal(str(value)).quantize(Decimal('.00000001')))


def sync_account(conn, account: dict) -> None:
    """Called under the writer's account lock; no commit, no new financial action."""
    from .paper import snapshot
    # Do not advance ownership/performance scans as a side effect of saving a
    # financial account. Historical evidence is handled by our own projection.
    view = snapshot(account, project=False, conn=conn)
    from .paper_policy import account_facts
    reasons = {fact['classification'] for fact in account_facts(account).values() if fact['classification'] != 'binance'}
    if any(not item.get('marked_at') for item in [*account['positions'].values(), *account['spot'].values()]):
        reasons.add('missing_price_observation')
    current = {key: view.get(key) for key in CURRENT_FIELDS}
    if reasons:
        for key in ('net_equity', 'unrealized_pnl', 'performance_pnl'):
            current[key] = None
    if account.get('competition_capital') is None:
        reasons.add('paper_capital_unverified')
        current['performance_pnl'] = None
    current.update(oldest_mark_at=view.get('oldest_mark_at'), valuation_status='incomplete' if reasons else view['valuation_status'],
                   reason_codes=sorted(reasons),
                   ledger_fees=account.get('fees'), ledger_realized=account.get('realized_pnl'),
                   ledger_interest=account.get('interest_paid'),
                   enabled=account['enabled'], bankrupt=account.get('account_status') == 'bankrupt')
    revision = conn.execute("SELECT revision FROM paper_raw_versions WHERE namespace=%s AND user_id=%s AND kind='account' AND record_id=%s",
                            (account['namespace'], account['user_id'], account['user_id'])).fetchone()
    conn.execute('''INSERT INTO paper_statistics_accounts
        (namespace,user_id,source_updated_at,current_epoch,complete,current_data,current_revision)
        VALUES(%s,%s,%s,%s,NOT EXISTS(SELECT 1 FROM paper_events WHERE namespace=%s AND user_id=%s),%s,%s)
        ON CONFLICT(namespace,user_id) DO UPDATE SET source_updated_at=excluded.source_updated_at,
            current_epoch=excluded.current_epoch,current_data=excluded.current_data,current_revision=excluded.current_revision''',
        (account['namespace'], account['user_id'], account.get('updated_at', account['created_at']),
         account.get('account_epoch'), account['namespace'], account['user_id'], json.dumps(current), revision[0] if revision else 0))


def _cycle(conn, event, epoch, fill, liquidation):
    """Allocate remaining entry fees at each actual exit, including partial exits."""
    ns, user = event['namespace'], event['user_id']
    action = fill.get('action')
    entry = action in {'buy', 'open_long', 'open_short'}
    valid_action = entry or action in {'sell', 'close_long', 'close_short'} or liquidation
    fee = _decimal(fill.get('fee'))
    if fee is not None and fee < 0:
        fee = None
    amount = _decimal(fill.get('amount'))
    realized = _decimal(fill.get('realized_pnl'))
    origin = fill.get('cycle_id')
    venue_valid = fill.get('exchange') == 'binance'
    if not valid_action or not venue_valid:
        return fee if venue_valid else None, None, 'unverified_fill', 'unverified_fill', 'unverified_fill'
    if not origin or epoch is None or amount is None or amount <= 0:
        return fee, None, 'missing_cycle_evidence', 'missing_fee' if fee is None else None, None
    market = fill.get('market_type') or ('swap' if liquidation else None)
    side = fill.get('side') if liquidation else ('SHORT' if action.endswith('short') else 'LONG')
    key = hashlib.sha256(json.dumps([epoch, origin, market, fill.get('symbol'), side]).encode()).hexdigest()
    old = conn.execute('SELECT state FROM paper_statistics_cycles WHERE namespace=%s AND user_id=%s AND cycle_key=%s',
                       (ns, user, key)).fetchone()
    state = _json(old[0]) if old else {'quantity': '0', 'entry_fees': '0', 'net': '0',
                                      'first_event_id': event['id'], 'issue': None, 'closed_at': None}
    qty = Decimal(state['quantity'])
    pending = Decimal(state['entry_fees'])
    total = Decimal(state['net'])
    issue = state['issue']
    net = ZERO if entry else None
    if state['closed_at'] is not None:
        issue = 'reused_closed_cycle'
    if fee is None:
        issue = issue or 'missing_fee'
    if entry:
        qty += amount
        if fee is not None:
            pending += fee
    else:
        if qty <= 0 or amount - qty > max(Decimal('1e-12'), qty * Decimal('1e-12')):
            issue = issue or 'missing_entry_evidence'
        if realized is None:
            issue = issue or 'missing_realized_pnl'
        if qty > 0:
            allocated = pending * min(amount / qty, Decimal(1))
            pending -= allocated
            if not issue:
                net = realized - allocated  # Ledger realized_pnl already includes the EXIT fee.
                total += net
        qty -= amount
        if abs(qty) <= Decimal('1e-12'):
            qty = ZERO
            state['closed_at'] = event['timestamp']
    state.update(quantity=str(qty), entry_fees=str(pending), net=str(total), issue=issue)
    conn.execute('''INSERT INTO paper_statistics_cycles
        (namespace,user_id,cycle_key,epoch,first_event_id,last_event_id,closed_at,net,state)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,user_id,cycle_key)
        DO UPDATE SET last_event_id=excluded.last_event_id,closed_at=excluded.closed_at,net=excluded.net,state=excluded.state''',
        (ns, user, key, epoch, state['first_event_id'], event['id'], state['closed_at'],
         None if issue else total.quantize(Decimal('.00000001')), json.dumps(state)))
    return fee, net if not issue else None, issue, 'missing_fee' if fee is None else None, None


def project_event(conn, event: dict, cursor_epoch: int | None) -> int | None:
    """Process original event IDs once, in account order. Never inspect operation receipts."""
    ns, user, eid = event['namespace'], event['user_id'], event['id']
    data, kind = _json(event['payload']), event['kind']
    epoch = cursor_epoch
    if kind in {'create', 'bot_created'} and (kind == 'bot_created' or data.get('created') is True):
        epoch = 0 if epoch is None else epoch
    if kind == 'reset':
        supplied = data.get('account_epoch')
        epoch = supplied if type(supplied) is int and supplied >= 0 else None
        conn.execute('UPDATE paper_statistics_accounts SET cycle_start_id=%s WHERE namespace=%s AND user_id=%s', (eid, ns, user))
        # Reset is not a trade exit. Keep unfinished old cycles for audit.
        conn.execute("UPDATE paper_statistics_cycles SET state=jsonb_set(state,'{abandoned}','true') "
                     'WHERE namespace=%s AND user_id=%s AND closed_at IS NULL', (ns, user))
    if conn.execute('SELECT 1 FROM paper_statistics_events WHERE event_id=%s', (eid,)).fetchone():
        return epoch
    is_liquidation = kind == 'liquidation'
    fill = data.get('fill') or (data if is_liquidation else None)
    is_fill = isinstance(fill, dict) and bool(fill) and not is_liquidation
    fee, realized, interest = ZERO, ZERO, ZERO
    income_issue = fee_issue = count_issue = None
    if fill:
        fee, realized, income_issue, fee_issue, count_issue = _cycle(conn, event, epoch, fill, is_liquidation)
    elif kind == 'interest':
        interest = _decimal(data.get('interest'))
        if interest is None or interest < 0:
            interest, income_issue = None, 'missing_interest'
    elif kind in {'buy', 'sell', 'open_long', 'open_short', 'close_long', 'close_short', 'order_filled'}:
        fee = realized = None
        income_issue = fee_issue = count_issue = 'missing_fill_evidence'
    if epoch is None:
        income_issue = income_issue or 'unverified_account_epoch'
    conn.execute('''INSERT INTO paper_statistics_events
        (event_id,namespace,user_id,timestamp,epoch,kind,is_fill,is_liquidation,fee,realized_net,raw_realized,interest,income_issue,fee_issue,count_issue)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (eid, ns, user, event['timestamp'], epoch, kind, is_fill, is_liquidation,
         fee, realized, _decimal(fill.get('realized_pnl')) if fill else ZERO, interest, income_issue, fee_issue, count_issue))
    return epoch


def on_event(conn, account, event):
    ns, user = account['namespace'], account['user_id']
    state = conn.execute('SELECT complete,cursor_epoch FROM paper_statistics_accounts WHERE namespace=%s AND user_id=%s', (ns, user)).fetchone()
    if state and state['complete']:
        epoch = project_event(conn, event, state['cursor_epoch'])
        conn.execute('UPDATE paper_statistics_accounts SET cursor=%s,cursor_epoch=%s WHERE namespace=%s AND user_id=%s',
                     (event['id'], epoch, ns, user))
    # Events such as interest/liquidation can precede _save. The published current
    # view still belongs to this same transaction and will be refreshed by _save.


def rebuild_account(conn, account, limit=200):
    """Advance one historical page while the caller owns the account lock."""
    from .paper_storage import payload_page
    ns, user = account['namespace'], account['user_id']
    sync_account(conn, account)
    state = conn.execute('SELECT cursor,cursor_epoch FROM paper_statistics_accounts WHERE namespace=%s AND user_id=%s', (ns, user)).fetchone()
    events, progress = payload_page(conn, 'paper_events', 'namespace=%s AND user_id=%s', (ns, user),
                                    cursor=state['cursor'], count=limit)
    epoch = state['cursor_epoch']
    pending = [(event['id'], event) for event in events] + [(row['scan_id'], None) for row in progress['skipped_records']]
    for eid, event in sorted(pending):
        if event is not None:
            epoch = project_event(conn, event, epoch)
            continue
        # A missing event could be an entry, exit or reset. Subsequent evidence
        # cannot repair that uncertainty until a proven reset starts a new epoch.
        epoch = None
        original = conn.execute('SELECT timestamp,kind FROM paper_events WHERE id=%s', (eid,)).fetchone()
        conn.execute('''INSERT INTO paper_statistics_events
            (event_id,namespace,user_id,timestamp,epoch,kind,income_issue,fee_issue,count_issue)
            VALUES(%s,%s,%s,%s,%s,%s,'unreadable_event','unreadable_event','unreadable_event')
            ON CONFLICT(event_id) DO NOTHING''', (eid, ns, user, original['timestamp'], None, original['kind']))
    conn.execute('UPDATE paper_statistics_accounts SET cursor=%s,cursor_epoch=%s,complete=%s WHERE namespace=%s AND user_id=%s',
                 (progress['next_cursor'], epoch, not progress['has_more'], ns, user))


def maintain_statistics():
    """Fair, resumable background backfill; no request waits for whole history."""
    from .paper import _get
    started = time.monotonic()
    with store.connection() as conn:
        if not try_lock_scope(conn, 'paper-statistics-maintenance'):
            return
        saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='statistics:accounts'").fetchone()
        cursor = saved[0] if saved else 0
        rows = conn.execute('SELECT storage_id,namespace,user_id FROM paper_accounts WHERE storage_id>%s ORDER BY storage_id LIMIT 10', (cursor,)).fetchall()
        for row in rows:
            if time.monotonic() - started > 2:
                break
            if try_lock_scope(conn, 'paper-account', row['namespace'], row['user_id']):
                account = _get(conn, row['namespace'], row['user_id'])
                rebuild_account(conn, account)
            cursor = row['storage_id']
        if not rows:
            cursor = 0
        conn.execute("INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES('statistics:accounts',%s,'{}') "
                     'ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor', (cursor,))
        conn.commit()


def statistics(namespace=None, user_id=None, period: Period = 'current_cycle') -> dict:
    """Aggregate the entire authorized filter, independent of detail pagination."""
    from .paper import identity
    if period not in PERIODS:
        raise ValueError('Unsupported statistics period')
    clauses, params = [], []
    for column, value in (('namespace', namespace), ('user_id', user_id)):
        if value is not None:
            clauses.append('a.' + column + '=%s')
            params.append(identity(value, column))
    where = ' AND '.join(clauses) or 'TRUE'
    end = time.time()
    start = end - (7 if period == '7d' else 30) * 86400 if period in {'7d', '30d'} else None
    with store.connection() as conn:
        # All queries observe one committed ledger/projection snapshot.
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        coverage = record(conn.execute('''SELECT COUNT(*) AS accounts,
            COUNT(*) FILTER(WHERE s.complete AND s.current_revision=COALESCE(v.revision,0)
                AND s.cursor=COALESCE((SELECT MAX(id) FROM paper_events e WHERE e.namespace=a.namespace AND e.user_id=a.user_id),0)) AS covered,
            MIN(s.source_updated_at) AS oldest_account_at
            FROM paper_accounts a LEFT JOIN paper_statistics_accounts s USING(namespace,user_id)
            LEFT JOIN paper_raw_versions v ON v.namespace=a.namespace AND v.user_id=a.user_id AND v.kind='account' AND v.record_id=a.user_id
            WHERE ''' + where, params).fetchone())
        base = ' FROM paper_statistics_accounts a WHERE ' + where
        columns = [f"SUM((current_data->>'{key}')::numeric) AS {key}, COUNT(*) FILTER(WHERE current_data->>'{key}' IS NULL) AS missing_{key}" for key in CURRENT_FIELDS]
        current = record(conn.execute('SELECT ' + ','.join(columns) + ",MIN((current_data->>'oldest_mark_at')::double precision) AS oldest_mark_at," 
            " COUNT(*) FILTER(WHERE (current_data->>'enabled')::boolean) AS enabled_accounts," 
            " COUNT(*) FILTER(WHERE (current_data->>'bankrupt')::boolean) AS bankrupt_accounts" + base, params).fetchone())
        conditions = [where, 'e.timestamp<=%s']
        history_params = [*params, end]
        if period == 'current_cycle':
            conditions.append('e.event_id>=a.cycle_start_id')
        elif start is not None:
            conditions.append('e.timestamp>=%s')
            history_params.append(start)
        history = record(conn.execute('''SELECT
            MIN(e.timestamp) AS first_event_at,
            COALESCE(SUM(e.realized_net),0) AS realized_net, COALESCE(SUM(e.fee),0) AS fees,
            COALESCE(SUM(e.interest),0) AS interest_paid,
            COUNT(*) FILTER(WHERE is_fill) AS fill_count, COUNT(*) FILTER(WHERE is_liquidation) AS liquidation_count,
            COUNT(*) FILTER(WHERE income_issue IS NOT NULL AND kind<>'interest') AS income_missing,
            COUNT(*) FILTER(WHERE fee_issue IS NOT NULL) AS fees_missing,
            COUNT(*) FILTER(WHERE count_issue IS NOT NULL) AS counts_missing,
            COUNT(*) FILTER(WHERE interest IS NULL AND kind='interest') AS interest_missing
            FROM paper_statistics_events e JOIN paper_statistics_accounts a USING(namespace,user_id)
            WHERE ''' + ' AND '.join(conditions), history_params).fetchone())
        cycle_conditions = [where, 'c.closed_at<=%s']
        cycle_params = [*params, end]
        if period == 'current_cycle':
            cycle_conditions.append('c.epoch=a.current_epoch')
        elif start is not None:
            cycle_conditions.append('c.closed_at>=%s')
            cycle_params.append(start)
        cycles = record(conn.execute('''SELECT COUNT(*) AS closed_count,
            COUNT(*) FILTER(WHERE net>0) AS win_count, COUNT(*) FILTER(WHERE net<0) AS loss_count,
            COUNT(*) FILTER(WHERE net=0) AS breakeven_count,
            COUNT(*) FILTER(WHERE net IS NULL) AS missing
            FROM paper_statistics_cycles c JOIN paper_statistics_accounts a USING(namespace,user_id)
            WHERE ''' + ' AND '.join(cycle_conditions), cycle_params).fetchone())
        reasons = [r[0] for r in conn.execute('''SELECT DISTINCT reason FROM paper_statistics_events e
            JOIN paper_statistics_accounts a USING(namespace,user_id)
            CROSS JOIN LATERAL unnest(ARRAY[e.income_issue,e.fee_issue,e.count_issue]) reason
            WHERE reason IS NOT NULL AND ''' + ' AND '.join(conditions), history_params)]
        reasons.extend(r[0] for r in conn.execute('''SELECT DISTINCT reason FROM paper_statistics_accounts a
            CROSS JOIN LATERAL jsonb_array_elements_text(a.current_data->'reason_codes') reason
            WHERE ''' + where, params))
        unknown_epoch = conn.execute('SELECT COUNT(*)' + base + ' AND (current_epoch IS NULL OR cursor_epoch IS NULL OR current_epoch<>cursor_epoch)', params).fetchone()[0]
        current_history_missing = conn.execute('''SELECT COUNT(*) FROM paper_statistics_events e
            JOIN paper_statistics_accounts a USING(namespace,user_id) WHERE ''' + where + ''' AND e.event_id>=a.cycle_start_id
            AND e.income_issue IN ('unverified_fill','unreadable_event','unverified_account_epoch')''', params).fetchone()[0]
        # Detect missing historical financial events even when a create/reset
        # survives: a reconstructed period must reconcile to the current ledger.
        mismatched = conn.execute('''SELECT COUNT(*) FROM paper_statistics_accounts a
            LEFT JOIN LATERAL (SELECT COALESCE(SUM(fee),0) fees, COALESCE(SUM(raw_realized),0) realized,
                COALESCE(SUM(interest),0) interest, GREATEST(COUNT(*),1)*0.00000001 tolerance,
                COUNT(*) FILTER(WHERE income_issue IS NOT NULL OR fee_issue IS NOT NULL) issues
                FROM paper_statistics_events e WHERE e.namespace=a.namespace AND e.user_id=a.user_id
                    AND e.event_id>=a.cycle_start_id) totals ON TRUE
            WHERE ''' + where + ''' AND totals.issues=0 AND (
                ABS((a.current_data->>'ledger_fees')::numeric-totals.fees)>tolerance OR
                ABS((a.current_data->>'ledger_realized')::numeric-totals.realized)>tolerance OR
                ABS((a.current_data->>'ledger_interest')::numeric-totals.interest)>tolerance)''', params).fetchone()[0]
    ready = coverage['covered'] == coverage['accounts']
    if not ready:
        reasons.append('history_processing')
    if unknown_epoch:
        reasons.append('unverified_account_epoch')
    if mismatched:
        reasons.append('ledger_history_mismatch')
    current_values = {key: _number(current[key] or ZERO) if ready and not current['missing_' + key] else None for key in CURRENT_FIELDS}
    if current_history_missing or unknown_epoch:
        # An unknown starting cycle prevents proving cycle profit, but does not
        # erase independently verified cash or currently valued holdings.
        current_values['performance_pnl'] = None
    if ready and current_values['net_equity'] is None:
        reasons.append('current_valuation_incomplete')
    current_values.update(account_count=coverage['accounts'], enabled_accounts=current['enabled_accounts'] if ready else None,
                          bankrupt_accounts=current['bankrupt_accounts'] if ready else None,
                          oldest_mark_at=current['oldest_mark_at'], valuation_status='complete' if ready and current_values['net_equity'] is not None else 'incomplete')
    income_ok = ready and not history['income_missing'] and not unknown_epoch and not mismatched
    wins_ok = income_ok and not cycles['missing']
    performance = dict(
        realized_net=_number(history['realized_net']) if income_ok else None,
        realized_net_after_interest=_number(history['realized_net'] - history['interest_paid']) if income_ok and not history['interest_missing'] else None,
        fees=_number(history['fees']) if ready and not history['fees_missing'] and not unknown_epoch and not mismatched else None,
        interest_paid=_number(history['interest_paid']) if ready and not history['interest_missing'] and not unknown_epoch and not mismatched else None,
        fill_count=history['fill_count'] if ready and not history['counts_missing'] and not unknown_epoch and not mismatched else None,
        liquidation_count=history['liquidation_count'] if ready and not history['counts_missing'] and not unknown_epoch and not mismatched else None,
        closed_count=cycles['closed_count'] if wins_ok else None,
        win_count=cycles['win_count'] if wins_ok else None,
        loss_count=cycles['loss_count'] if wins_ok else None,
        breakeven_count=cycles['breakeven_count'] if wins_ok else None,
        win_rate=round(cycles['win_count'] / cycles['closed_count'] * 100, 4) if wins_ok and cycles['closed_count'] else None)
    label = {'current_cycle': '本轮', '7d': '近7天', '30d': '近30天', 'all': '全部历史'}[period]
    if not ready:
        summary = '统计正在补算历史，完整结果尚未就绪。'
    elif performance['closed_count'] is None:
        summary = label + '部分历史证据不足，相关收益与胜率无法计算；可用指标单独展示。'
    elif not performance['closed_count']:
        summary = label + '暂无完整结束的交易，胜率暂不计算。'
    else:
        summary = f"{label}已结束{performance['closed_count']}笔，盈利{performance['win_count']}笔，胜率{performance['win_rate']:g}%。"
    return dict(execution_mode='paper', currency='USDT', scope={'namespace': namespace, 'user_id': user_id, 'type': 'all_matching_accounts'},
                period=period, period_start=start if start is not None else history['first_event_at'] if ready else None,
                period_end=end, current_cycle_per_account=period == 'current_cycle',
                observed_at=end, coverage=coverage, status='processing' if not ready else 'partial' if reasons or current_values['net_equity'] is None else 'complete',
                reason_codes=sorted(set(reasons)), current=current_values, performance=performance, summary=summary)
