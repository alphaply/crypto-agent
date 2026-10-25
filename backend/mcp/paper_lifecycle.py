"""Account lifecycle and atomic finances. Callers hold the account write locks."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import time
from functools import wraps

from backend.storage import lock_scope
from . import store

execution_epoch = ContextVar('paper_execution_epoch', default=None)
BEIJING = timezone(timedelta(hours=8))
UNIT = Decimal('.00000001')


def initialize(account: dict, conn=None) -> None:
    from . import paper
    account.setdefault('account_epoch', 0)
    account.setdefault('reset_count', 0)
    account.setdefault('account_status', 'active')
    # An upgraded ledger has no pre-upgrade interest liability.
    account.setdefault('interest_cursor', time.time())
    account.setdefault('interest_accrued', '0')
    if 'interest_rate_at_cursor' not in account:
        account['interest_rate_at_cursor'] = str(paper.settings(conn)['daily_interest_rate'])
    account.setdefault('interest_paid', 0)


def interest_reserve(account: dict, conn=None) -> dict | None:
    """Project unpaid interest without settling, reserving cash or changing status."""
    from . import paper
    principal = paper.number(account['debt'])
    accrued = Decimal(account['interest_accrued'])
    if principal == 0 and accrued <= 0:
        return None
    if conn is None:
        with store.connection() as conn:
            return interest_reserve(account, conn)
    cursor = account['interest_cursor']
    now = max(time.time(), cursor)
    settlement = (datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
                  + timedelta(days=1)).timestamp()
    rate = Decimal(account['interest_rate_at_cursor'])
    latest = conn.execute('SELECT daily_rate FROM paper_bank_rates WHERE effective_at<=%s '
                          'ORDER BY effective_at DESC,revision DESC LIMIT 1', (cursor,)).fetchone()
    if latest:
        rate = Decimal(str(latest[0]))
    changes = conn.execute('SELECT effective_at,daily_rate FROM paper_bank_rates WHERE effective_at>%s '
                           'AND effective_at<=%s ORDER BY effective_at,revision', (cursor, now)).fetchall()
    overdue = Decimal(0)
    estimate = Decimal(0)
    # Writes settle before changing principal. Only rates can change since the
    # stored cursor. Follow the same daily rounding and carry as settle().
    for boundary, next_rate in [*[(r[0], Decimal(str(r[1]))) for r in changes], (settlement, None)]:
        while cursor < boundary:
            day_end = (datetime.fromtimestamp(cursor, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
                       + timedelta(days=1)).timestamp()
            end = min(boundary, day_end)
            accrued += principal * rate * Decimal(str(end - cursor)) / Decimal(86400)
            cursor = end
            if cursor == day_end:
                charged = accrued.quantize(UNIT)
                if day_end <= now:
                    overdue += charged
                else:
                    estimate = charged
                accrued -= charged
        if next_rate is not None:
            rate = next_rate
    daily = principal * rate
    required = overdue + estimate
    if daily <= 0 and required <= 0:
        return None
    available = max(paper.signed_cash(account['cash']) - paper.number(account.get('reserved_cash', 0)), Decimal(0))
    shortfall = paper.money(max(required - available, Decimal(0)))

    def amount(value):
        return f'{paper.money(value):.8f}'.rstrip('0').rstrip('.')

    message = (f'请预留 USDT 支付借款利息：按当前本金和日利率，完整一天约 {amount(daily)} U，'
               f'下次北京时间零点预计扣息 {amount(estimate)} U；扣除挂单占用后可用现金 {amount(available)} U。')
    if overdue > 0:
        message += f'另有已到期未扣利息 {amount(overdue)} U，合计预计需预留 {amount(required)} U。'
    if shortfall > 0:
        message += f'预计还差 {amount(shortfall)} U，请留足现金或调整挂单。'
    message += '这是提醒，不冻结资金；借还款或调息会改变实际扣息金额。'
    return {'currency': 'USDT', 'daily_interest_rate': float(rate),
            'estimated_daily_interest': paper.money(daily), 'estimated_next_interest': paper.money(estimate),
            'overdue_interest': paper.money(overdue), 'estimated_required_cash': paper.money(required),
            'next_settlement_at': settlement, 'available_cash': paper.money(available), 'shortfall': shortfall,
            'advisory_only': True, 'message': message}


def recover_bankruptcy(conn, account: dict) -> None:
    """Repair a proven legacy misclassification under the account write lock."""
    from . import paper
    from .paper_storage import payload_page, history_limit
    if account['account_status'] != 'bankrupt' or account.get('bankruptcy_reason') == 'insufficient_cash_for_interest':
        return
    namespace, user_id = account['namespace'], account['user_id']
    boundaries = conn.execute("SELECT COALESCE(MAX(id) FILTER (WHERE kind='reset'),0), "
                              "COALESCE(MAX(id) FILTER (WHERE kind='bankruptcy_recovered'),0) "
                              "FROM paper_events WHERE namespace=%s AND user_id=%s "
                              "AND kind IN ('reset','bankruptcy_recovered')", (namespace, user_id)).fetchone()
    boundary, last_recovery = boundaries
    events, progress = payload_page(conn, 'paper_events',
                                   "namespace=%s AND user_id=%s AND kind IN ('interest','bankruptcy')",
                                   (namespace, user_id), cursor=boundary, count=history_limit())
    candidates = []
    causes = set()
    complete = not progress['has_more'] and not progress['skipped_records']
    for event in events:
        detail = event['payload']
        if event['kind'] == 'bankruptcy':
            if detail.get('reason') == 'insufficient_cash_for_interest':
                account['bankruptcy_reason'] = 'insufficient_cash_for_interest'
                paper._save(conn, account)
                return
            if event['id'] > last_recovery:
                causes.add(detail.get('reason'))
            continue
        if detail.get('account_epoch') != account['account_epoch']:
            complete = False
            continue
        try:
            cash_after = paper.signed_cash(detail['cash_after'])
            charged = paper.number(detail['interest'], positive=True)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            complete = False
            continue
        if cash_after < 0:
            # A later incoming transfer or the old unconditional position heal
            # cannot erase a real cash shortfall within this account cycle.
            account['bankruptcy_reason'] = 'insufficient_cash_for_interest'
            paper._save(conn, account)
            return
        if event['id'] > last_recovery and charged and cash_after < paper.number(account.get('reserved_cash', 0)):
            candidates.append(event['id'])
    if not complete:
        return
    reason = None
    if candidates and not causes and not account.get('bankruptcy_reason'):
        view = paper.snapshot(account, conn=conn)
        if paper.signed_cash(account['cash']) >= 0 and view['net_equity'] is not None and view['net_equity'] > 0:
            reason = 'interest_reservation_misclassification'
    if reason is None and (account.get('positions') or account.get('spot')) and causes <= {'nonpositive_net_equity'}:
        if account.get('bankruptcy_reason') in {None, 'nonpositive_net_equity'}:
            reason = 'active_positions_decoupled'
    if reason is None:
        return
    account['account_status'] = 'active'
    account.pop('bankruptcy_reason', None)
    paper._save(conn, account)
    paper._event(conn, account, 'bankruptcy_recovered', {
        'reason': reason, 'account_epoch': account['account_epoch'],
        'evidence_event_ids': candidates,
    })


def refresh_status(conn, account: dict) -> None:
    from . import paper
    if account['account_status'] == 'bankrupt':
        return
    if bool(account.get('positions')) or bool(account.get('spot')):
        return
    view = paper.snapshot(account, conn=conn)
    if view['net_equity'] is not None and view['net_equity'] <= 0:
        account['account_status'] = 'bankrupt'
        account['bankruptcy_reason'] = 'nonpositive_net_equity'
        paper._event(conn, account, 'bankruptcy', {'reason': 'nonpositive_net_equity', 'account_epoch': account['account_epoch']})


def assert_active(account: dict) -> None:
    if account.get('account_status') == 'bankrupt':
        raise ValueError('Account is bankrupt; only incoming transfers and an explicit 下海 reset are allowed')
    expected = execution_epoch.get()
    if expected is not None and expected != account['account_epoch']:
        raise ValueError('Account cycle changed; old trading intent cannot use reset funds')


def capture_epoch(function):
    """Bind pre-quote intent to its account cycle, including legacy single calls."""
    @wraps(function)
    def wrapped(caller, user_id, *args, **kwargs):
        if execution_epoch.get() is not None:
            return function(caller, user_id, *args, **kwargs)
        from . import paper
        namespace = paper.require(caller, 'trade')
        paper.require(caller, 'read')
        paper.identity(user_id)
        with store.connection() as conn:
            row = conn.execute("SELECT COALESCE((payload::jsonb->>'account_epoch')::bigint,0) "
                               'FROM paper_accounts WHERE namespace=%s AND user_id=%s', (namespace, user_id)).fetchone()
        if row is None:
            raise ValueError('Paper account does not exist')
        token = execution_epoch.set(row[0])
        try:
            return function(caller, user_id, *args, **kwargs)
        finally:
            execution_epoch.reset(token)
    return wrapped


def update_settings(payload) -> dict:
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    config = payload.model_dump()
    with store.connection() as conn:
        lock_scope(conn, 'paper-bank-policy')
        record_rate_change(conn, config)
        conn.execute("INSERT INTO mcp_records(kind,id,payload) VALUES('paper_settings','global',%s) "
                     'ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload', (json.dumps(config),))
        conn.commit()
    return config


def record_rate_change(conn, config: dict) -> None:
    from . import paper
    lock_scope(conn, 'paper-bank-policy')
    old = paper.settings(conn)
    if config['daily_interest_rate'] != old['daily_interest_rate']:
        conn.execute('INSERT INTO paper_bank_rates(effective_at,daily_rate) VALUES(%s,%s)',
                     (time.time(), str(config['daily_interest_rate'])))


def settle(conn, account: dict, now: float | None = None) -> None:
    from . import paper
    # A rate revision must commit before a settlement can move past its
    # effective timestamp, or a concurrent edit could be missed forever.
    lock_scope(conn, 'paper-bank-policy')
    initialize(account, conn)
    now = time.time() if now is None else now
    cursor = account['interest_cursor']
    if now <= cursor:
        return
    accrued = Decimal(account['interest_accrued'])
    rate = Decimal(account['interest_rate_at_cursor'])
    latest = conn.execute('SELECT daily_rate FROM paper_bank_rates WHERE effective_at<=%s '
                          'ORDER BY effective_at DESC,revision DESC LIMIT 1', (cursor,)).fetchone()
    if latest:
        rate = Decimal(str(latest[0]))
    rates = conn.execute('SELECT effective_at,daily_rate FROM paper_bank_rates WHERE effective_at>%s '
                         'AND effective_at<=%s ORDER BY effective_at,revision', (cursor, now)).fetchall()
    changes = [(r[0], Decimal(str(r[1]))) for r in rates]
    principal = paper.number(account['debt'])
    while cursor < now:
        day_end = (datetime.fromtimestamp(cursor, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
                   + timedelta(days=1)).timestamp()
        boundary = min(now, day_end, changes[0][0] if changes else now)
        accrued += principal * rate * Decimal(str(boundary - cursor)) / Decimal(86400)
        cursor = boundary
        if cursor == day_end:
            charged = accrued.quantize(UNIT)
            inserted = conn.execute('INSERT INTO paper_interest_settlements(namespace,user_id,account_epoch,period_end,amount) '
                                    'VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING period_end',
                                    (account['namespace'], account['user_id'], account['account_epoch'], day_end, charged)).fetchone()
            if inserted and charged:
                cash = paper.signed_cash(account['cash'])
                cash_shortfall = charged > cash
                newly_insolvent = cash_shortfall and account.get('bankruptcy_reason') != 'insufficient_cash_for_interest'
                if cash_shortfall:
                    account['account_status'] = 'bankrupt'
                    account['bankruptcy_reason'] = 'insufficient_cash_for_interest'
                account['cash'] = paper.money(cash - charged)
                account['interest_paid'] = paper.money(Decimal(str(account['interest_paid'])) + charged)
                paper._event(conn, account, 'interest', {'interest': paper.money(charged), 'period_end': day_end,
                                                       'account_epoch': account['account_epoch']})
                if newly_insolvent:
                    paper._event(conn, account, 'bankruptcy', {
                        'reason': 'insufficient_cash_for_interest', 'interest': paper.money(charged),
                        'cash_before': paper.money(cash), 'period_end': day_end,
                        'account_epoch': account['account_epoch'],
                    })
            accrued -= charged
        while changes and changes[0][0] == cursor:
            _, rate = changes.pop(0)
    account.update(interest_cursor=now, interest_accrued=str(accrued), interest_rate_at_cursor=str(rate))


def _record(conn, caller, account, operation_id, fingerprint, result):
    from . import paper
    encoded = json.dumps(result, allow_nan=False)
    conn.execute('INSERT INTO paper_operations(namespace,user_id,operation_id,fingerprint,principal,result) VALUES(%s,%s,%s,%s,%s,%s)',
                 (account['namespace'], account['user_id'], operation_id, fingerprint, caller['id'], encoded))
    from .paper_storage import receipt_revision
    receipt_revision(conn, account['namespace'], account['user_id'], operation_id, encoded)
    paper._save(conn, account)


@capture_epoch
def transfer(caller, user_id, recipient_user_id, amount, operation_id):
    from . import paper
    from .paper_orders import refresh_reservations
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    namespace = paper.require(caller, 'trade')
    paper.require(caller, 'read')
    paper.identity(user_id)
    paper.identity(recipient_user_id)
    if user_id == recipient_user_id:
        raise ValueError('Transfer requires two different accounts')
    amount = paper.number(amount, positive=True)
    if amount != amount.quantize(UNIT):
        raise ValueError('Transfer amounts support up to 8 decimal places')
    request = {'action': 'transfer', 'recipient_user_id': recipient_user_id, 'amount': paper.money(amount)}
    with store.connection() as conn:
        for target in sorted((user_id, recipient_user_id)):
            lock_scope(conn, 'paper-account', namespace, target)
        fingerprint, previous = paper._receipt(conn, namespace, user_id, operation_id, request)
        if previous is not None:
            return previous
        sender = paper._get(conn, namespace, user_id)
        recipient = paper._get(conn, namespace, recipient_user_id)
        for account in (sender, recipient):
            refresh_reservations(conn, account)
            settle(conn, account)
            refresh_status(conn, account)
        if sender['account_status'] == 'bankrupt':
            for account in (sender, recipient):
                paper._save(conn, account)
            conn.commit()
            assert_active(sender)
        assert_active(sender)
        if not sender['enabled']:
            raise ValueError('Sender account is disabled')
        if amount > paper.signed_cash(sender['cash']) - paper.number(sender['reserved_cash']):
            raise ValueError('Transfer exceeds available cash')
        sender['cash'] = paper.money(paper.signed_cash(sender['cash']) - amount)
        recipient['cash'] = paper.money(paper.signed_cash(recipient['cash']) + amount)
        for account, flow in ((sender, -amount), (recipient, amount)):
            if account.get('competition_capital') is not None:
                account['competition_capital'] = paper.money(Decimal(str(account['competition_capital'])) + flow)
        conn.execute('INSERT INTO paper_transfers(namespace,sender,operation_id,recipient,amount,sender_epoch,recipient_epoch) '
                     'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                     (namespace, user_id, operation_id, recipient_user_id, amount, sender['account_epoch'], recipient['account_epoch']))
        details = {'amount': paper.money(amount), 'currency': 'USDT', 'sender_user_id': user_id, 'recipient_user_id': recipient_user_id}
        paper._event(conn, sender, 'transfer_out', details, operation_id)
        paper._event(conn, recipient, 'transfer_in', details, operation_id)
        refresh_status(conn, sender)
        view = paper.snapshot(sender, project=False, conn=conn)
        result = {'status': 'completed', 'action': 'transfer', 'currency': 'USDT',
                  'operation_id': operation_id, **details, 'cash_after': view['cash'],
                  'available_cash': view['available_cash'], 'reserved_cash': view['reserved_cash'],
                  'account': view}
        if view['interest_reserve'] is not None:
            result['interest_reserve'] = view['interest_reserve']
        _record(conn, caller, sender, operation_id, fingerprint, result)
        paper._save(conn, recipient)
        conn.commit()
    return result


def reset(caller, user_id, operation_id):
    from . import paper
    from .paper_orders import rows, finish
    def change(account, conn):
        if account['account_type'] == 'bot' or account['account_status'] != 'bankrupt':
            raise ValueError('Only a bankrupt user can request 下海')
        epoch, count = account['account_epoch'] + 1, account['reset_count'] + 1
        account.pop('bankruptcy_reason', None)
        for order in rows(conn, account['namespace'], account['user_id']):
            finish(conn, account, order, 'cancelled', 'Account reset: old cycle ended')
        cfg = paper.settings(conn)
        account.update(name=user_id, cash=paper.money(cfg['initial_balance']), debt=0, loan_limit=cfg['loan_limit'],
                       leverage=1, markets=['spot', 'swap'], enabled=True, spot={}, positions={},
                       realized_pnl=0, fees=0, reserved_cash=0, reserved_spot={},
                       initial_capital=cfg['initial_balance'], competition_capital=cfg['initial_balance'],
                       leaderboard_visible=True, notifications_enabled=True, account_status='active',
                       account_epoch=epoch, reset_count=count, interest_cursor=time.time(), interest_accrued='0',
                       interest_rate_at_cursor=str(cfg['daily_interest_rate']), interest_paid=0)
        return {'reset_count': count, 'account_epoch': epoch}
    return paper._write(caller, user_id, operation_id, {'action': 'reset'}, change)


def maintenance_finances():
    """Cursor scans include debt-only and frozen accounts, without network I/O."""
    from . import paper
    from .paper_storage import payload_page, batch_size
    if not paper.settings()['enabled']:
        return
    with store.connection() as conn:
        lock_scope(conn, 'paper-finance-scan')
        row = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='finances'").fetchone()
        accounts, progress = payload_page(conn, 'paper_accounts', '1=1', (), cursor=row[0] if row else 0, count=batch_size())
        for item in sorted(accounts, key=lambda r: (r['namespace'], r['user_id'])):
            lock_scope(conn, 'paper-account', item['namespace'], item['user_id'])
        for item in accounts:
            account = paper._get(conn, item['namespace'], item['user_id'])
            settle(conn, account)
            refresh_status(conn, account)
            paper._save(conn, account)
        conn.execute("INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES('finances',%s,'{}') "
                     'ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor',
                     (progress['next_cursor'] if progress['has_more'] else 0,))
        conn.commit()


from .paper_storage import bounded
transfer = bounded(transfer)
maintenance_finances = bounded(maintenance_finances)
