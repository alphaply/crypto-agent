"""User-scoped paper ledger. No exchange credentials or private trading APIs."""
from __future__ import annotations
from backend.storage import lock_scope

import asyncio
import json
import logging
import re
import time
import uuid
from functools import partial
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import store
from .resilience import run

logger = logging.getLogger('PaperTrading')
Money = Decimal
ZERO = Money('0')


class StoredPaperSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    initial_balance: float = Field(10000, ge=0, le=1e12, allow_inf_nan=False)
    max_leverage: int = Field(125, ge=1, le=125)
    loan_limit: float = Field(1000000, ge=0, le=1e12, allow_inf_nan=False)
    bank_enabled: bool = True
    daily_interest_rate: float = Field(0, ge=0, allow_inf_nan=False)
    fee_rate: float = Field(.001, ge=0, le=.01, allow_inf_nan=False)
    maintenance_margin_rate: float = Field(.005, gt=0, le=.1, allow_inf_nan=False)
    exchange: Literal['binance', 'okx', 'bitget', 'bybit'] = 'binance'
    bot_user_id: str = Field('bot:xiaojin-mengzhizi', pattern=r'^bot:[a-zA-Z0-9_.-]{1,80}$')
    bot_name: str = Field('小金萌智子', min_length=1, max_length=100)
    venue_policy_version: Literal[1] = 1


class PaperSettings(StoredPaperSettings):
    exchange: Literal['binance'] = 'binance'


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(None, max_length=100)
    cash: float | None = Field(None, ge=-1e12, le=1e12, allow_inf_nan=False)
    debt: float | None = Field(None, ge=0, le=1e12, allow_inf_nan=False)
    loan_limit: float | None = Field(None, ge=0, le=1e12, allow_inf_nan=False)
    leverage: int | None = Field(None, ge=1, le=125)
    markets: list[Literal['spot', 'swap']] | None = None
    enabled: bool | None = None
    leaderboard_visible: bool | None = None
    notifications_enabled: bool | None = None

    @model_validator(mode='after')
    def valid_markets(self):
        if self.markets is not None and (not self.markets or len(set(self.markets)) != len(self.markets)):
            raise ValueError('Select unique spot and/or swap markets')
        return self


def settings(conn=None) -> dict:
    if conn is None:
        payload = store.get('paper_settings', 'global')
    else:
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind='paper_settings' AND id='global'").fetchone()
        payload = json.loads(row[0]) if row else {}
    raw = StoredPaperSettings.model_validate(payload or {}).model_dump()
    return PaperSettings.model_validate({**raw, 'exchange': 'binance'}).model_dump()


def identity(value: str, label='user_id') -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.:@-]{1,100}', value):
        raise ValueError(f'{label} must be a 1–100 character stable identifier; QQ IDs must be strings')
    return value


def require(caller: dict, scope='read') -> str:
    from .service import require as mcp_require
    mcp_require(caller, scope)
    if caller.get('key_type') != 'paper':
        raise PermissionError('A dedicated paper API key is required')
    if not settings()['enabled']:
        raise PermissionError('Paper trading is disabled')
    return identity(caller.get('paper_namespace'), 'paper_namespace')


def number(value, *, positive=False) -> Money:
    if isinstance(value, bool):
        raise ValueError('A finite numeric amount is required')
    try:
        result = Money(str(value))
    except Exception as exc:
        raise ValueError('A finite numeric amount is required') from exc
    if not result.is_finite() or result < 0 or result > Money('1e15') or (positive and result <= 0):
        raise ValueError('Amount is outside the supported range')
    return result


def signed_cash(value) -> Money:
    if isinstance(value, bool):
        raise ValueError('A finite cash balance is required')
    result = Money(str(value))
    if not result.is_finite() or abs(result) > Money('1e15'):
        raise ValueError('Cash balance is outside the supported range')
    return result


def money(value) -> float:
    result = Money(str(value))
    if not result.is_finite() or abs(result) > Money('1e15'):
        raise ValueError('Computed amount is outside the supported range')
    return float(result.quantize(Money('.00000001'), rounding=ROUND_HALF_UP))


def _get(conn, namespace, user_id):
    from .paper_storage import one
    account = one(conn, 'paper_accounts', 'payload', 'namespace=%s AND user_id=%s', (namespace, user_id), identity='paper account')
    if account is None:
        raise ValueError('Paper account does not exist; call create_paper_account first')
    return upgrade_account(conn, account)


def _save(conn, account):
    account['updated_at'] = time.time()
    encoded = json.dumps({k: v for k, v in account.items() if not k.startswith('_')}, allow_nan=False)
    conn.execute('INSERT INTO paper_accounts(namespace,user_id,payload) VALUES(%s,%s,%s) ON CONFLICT(namespace,user_id) DO UPDATE SET payload=excluded.payload',
                 (account['namespace'], account['user_id'], encoded))


def upgrade_account(conn, account):
    from .paper_lifecycle import initialize, recover_bankruptcy
    repair_locked = False
    if account.get('account_status') == 'bankrupt':
        from backend.storage import try_lock_scope
        from .paper_storage import one
        # Readers and multi-account scans may hold stale snapshots. Do not wait
        # out of account order or overwrite a concurrent transfer/settlement.
        repair_locked = try_lock_scope(conn, 'paper-account', account['namespace'], account['user_id'])
        if repair_locked:
            current = one(conn, 'paper_accounts', 'payload', 'namespace=%s AND user_id=%s',
                          (account['namespace'], account['user_id']), identity='bankruptcy repair account')
            if current is not None:
                account.clear()
                account.update(current)
    initialize(account, conn)
    if 'initial_capital' not in account:
        from .paper_storage import one, payload_page, history_limit
        row = conn.execute('SELECT operation_id FROM paper_operations WHERE namespace=%s AND user_id=%s ORDER BY storage_id LIMIT 1',
                           (account['namespace'], account['user_id'])).fetchone()
        original = one(conn, 'paper_operations', 'result', 'namespace=%s AND user_id=%s AND operation_id=%s',
                       (account['namespace'], account['user_id'], row[0]), identity='initial capital evidence') if row else None
        account['initial_capital'] = (original or {}).get('account', {}).get('cash')
        # Historical admin adjustments are funding, not trading performance.
        capital = Money(str(account['initial_capital'])) if account['initial_capital'] is not None else None
        old_cash, old_debt = account['initial_capital'], 0
        events, progress = payload_page(conn, 'paper_events', 'namespace=%s AND user_id=%s',
                                        (account['namespace'], account['user_id']), count=history_limit())
        for event in events:
            data = event['payload']
            if event['kind'] == 'admin_adjustment' and capital is not None and old_cash is not None:
                capital += Money(str(data.get('cash', old_cash))) - Money(str(old_cash))
                capital -= Money(str(data.get('debt', old_debt))) - Money(str(old_debt))
            # New events contain cash/debt after each mutation for future audits.
            old_cash, old_debt = data.get('cash_after', data.get('cash', old_cash)), data.get('debt_after', data.get('debt', old_debt))
            if event['operation_id']:
                receipt = one(conn, 'paper_operations', 'result', 'namespace=%s AND user_id=%s AND operation_id=%s',
                              (account['namespace'], account['user_id'], event['operation_id']), identity='capital receipt')
                if receipt:
                    recorded = receipt.get('account', {})
                    old_cash, old_debt = recorded.get('cash', old_cash), recorded.get('debt', old_debt)
        account['competition_capital'] = money(capital) if capital is not None and progress['status'] == 'complete' else None
    account.setdefault('competition_capital', account['initial_capital'])
    account.setdefault('account_type', 'bot' if account['user_id'].startswith('bot:') else 'user')
    account.setdefault('leaderboard_visible', True)
    account.setdefault('notifications_enabled', True)
    account.setdefault('reserved_cash', 0)
    # Source proof is additive and separate from legacy fields, which may have
    # been filled by a previous upgrade. Never infer a venue or cycle here.
    from .paper_policy import account_facts, persist_origin
    facts = account_facts(account, conn)
    account['_venue_origins'] = {kind + ':' + key: fact for (kind, key), fact in facts.items()}
    for (kind, key), fact in facts.items():
        persist_origin(conn, account, kind, key, account[kind][key], fact)
    if repair_locked:
        recover_bankruptcy(conn, account)
    return account


def _event(conn, account, kind, details, operation_id=None):
    conn.execute('INSERT INTO paper_events(namespace,user_id,timestamp,kind,operation_id,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                 (account['namespace'], account['user_id'], time.time(), kind, operation_id,
                  json.dumps({**details, 'cash_after': account['cash'], 'debt_after': account['debt']}, allow_nan=False)))


def _receipt(conn, namespace, user_id, operation_id, request):
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
        raise ValueError('operation_id must be a stable 8–160 character identifier')
    fingerprint = store.digest(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False))
    row = conn.execute('SELECT fingerprint FROM paper_operations WHERE namespace=%s AND user_id=%s AND operation_id=%s',
                       (namespace, user_id, operation_id)).fetchone()
    if row and row['fingerprint'] != fingerprint:
        raise ValueError('operation_id was already used for a different paper request')
    if row is None:
        parent = conn.execute('SELECT 1 FROM paper_batches WHERE namespace=%s AND user_id=%s AND operation_id=%s',
                              (namespace, user_id, operation_id)).fetchone()
        if parent:
            raise ValueError('operation_id already belongs to a paper batch parent')
        reserved = conn.execute('SELECT parent_operation_id,action_index FROM paper_batch_children '
                                'WHERE namespace=%s AND user_id=%s AND child_operation_id=%s',
                                (namespace, user_id, operation_id)).fetchone()
        if reserved:
            from .paper_batch import _step
            current = _step.get()
            if current is None or current[:4] != (namespace, user_id, reserved['parent_operation_id'], reserved['action_index']):
                raise ValueError('operation_id belongs to a reserved paper batch child')
    from .paper_storage import one
    previous = one(conn, 'paper_operations', 'result', 'namespace=%s AND user_id=%s AND operation_id=%s',
                   (namespace, user_id, operation_id), identity='original paper receipt') if row else None
    return fingerprint, previous


def _write(caller, user_id, operation_id, request, change):
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    namespace = require(caller, 'trade')
    require(caller, 'read')
    identity(user_id)
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', namespace, user_id)
        fingerprint, previous = _receipt(conn, namespace, user_id, operation_id, request)
        if previous is not None:
            return previous
        account = _get(conn, namespace, user_id)
        from .paper_orders import refresh_reservations
        refresh_reservations(conn, account)
        from .paper_lifecycle import settle, refresh_status, assert_active
        settle(conn, account)
        refresh_status(conn, account)
        if request['action'] != 'reset':
            if account['account_status'] == 'bankrupt':
                _save(conn, account)
                conn.commit()
            assert_active(account)
        details = change(account, conn)
        refresh_status(conn, account)
        refresh_reservations(conn, account)
        _save(conn, account)
        accepted_orders = ([details['order']] if details.get('order') else details.get('orders', []))
        accepted = request['action'] in {'place_order', 'set_protection'} and any(order.get('state') == 'open' for order in accepted_orders)
        result = {'status': 'submitted' if accepted else 'completed', 'paper': True, 'operation_id': operation_id,
                  'account': snapshot(account, project=False, conn=conn), **details}
        entry_actions = {'buy', 'open_long', 'open_short'}
        if (request['action'] in entry_actions | {'set_leverage', 'add_margin', 'reduce_margin'}
                or request['action'] in {'place_order', 'edit_order'}
                and details.get('order', {}).get('action') in entry_actions):
            reminder = result['account']['interest_reserve']
            if reminder is not None:
                result['interest_reserve'] = reminder
        if accepted and any(order.get('order_type') in {'stop_loss', 'take_profit'} for order in accepted_orders):
            result.update(order_status='waiting_trigger', filled_amount=0)
        encoded = json.dumps(result, allow_nan=False)
        conn.execute('INSERT INTO paper_operations(namespace,user_id,operation_id,fingerprint,principal,result) VALUES(%s,%s,%s,%s,%s,%s)',
                     (namespace, user_id, operation_id, fingerprint, caller['id'], encoded))
        from .paper_storage import receipt_revision
        receipt_revision(conn, namespace, user_id, operation_id, encoded)
        _event(conn, account, request['action'], details, operation_id)
        # A batch child and its parent progress share this account commit.
        from .paper_batch import record_child_commit
        record_child_commit(conn, result)
        conn.commit()
    return result


def create_account(caller, user_id: str, operation_id: str, name: str = '') -> dict:
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    namespace = require(caller, 'trade')
    require(caller, 'read')
    identity(user_id)
    if len(name) > 100:
        raise ValueError('Name is too long')
    request = {'action': 'create', 'name': name}
    cfg = settings()
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', namespace, user_id)
        fingerprint, previous = _receipt(conn, namespace, user_id, operation_id, request)
        if previous is not None:
            return previous
        from .paper_storage import one
        existing = one(conn, 'paper_accounts', 'payload', 'namespace=%s AND user_id=%s', (namespace, user_id), identity='paper account')
        account = existing if existing is not None else {
            'namespace': namespace, 'user_id': user_id, 'name': name or user_id,
            'cash': money(cfg['initial_balance']), 'debt': 0, 'loan_limit': money(cfg['loan_limit']), 'leverage': 1,
            'markets': ['spot', 'swap'], 'enabled': True, 'spot': {}, 'positions': {},
            'realized_pnl': 0, 'fees': 0, 'created_at': time.time(),
            'initial_capital': money(cfg['initial_balance']), 'competition_capital': money(cfg['initial_balance']),
            'account_type': 'bot' if user_id.startswith('bot:') else 'user',
            'leaderboard_visible': True, 'notifications_enabled': True, 'reserved_cash': 0,
        }
        upgrade_account(conn, account)
        _save(conn, account)
        result = {'status': 'completed', 'paper': True, 'created': existing is None, 'operation_id': operation_id, 'account': snapshot(account, project=False, conn=conn)}
        conn.execute('INSERT INTO paper_operations(namespace,user_id,operation_id,fingerprint,principal,result) VALUES(%s,%s,%s,%s,%s,%s)',
                     (namespace, user_id, operation_id, fingerprint, caller['id'], json.dumps(result, allow_nan=False)))
        _event(conn, account, 'create', {'created': existing is None}, operation_id)
        conn.commit()
    return result


def snapshot(account: dict, *, project=True, conn=None) -> dict:
    used = sum((number(p['margin']) for p in account['positions'].values()), ZERO)
    pnl = ZERO
    positions = []
    for p in account['positions'].values():
        mark = number(p.get('mark_price', p['entry_price']), positive=True)
        unrealized = (mark - number(p['entry_price'])) * number(p['amount']) * (1 if p['side'] == 'LONG' else -1)
        pnl += unrealized
        positions.append({**p, 'unrealized_pnl': money(unrealized), 'margin_equity': money(number(p['margin']) + unrealized)})
    spot = []
    spot_value = ZERO
    for symbol, holding in account['spot'].items():
        value = number(holding['amount']) * number(holding.get('mark_price', holding['entry_price']))
        spot_value += value
        spot.append({'symbol': holding.get('symbol', symbol), **holding, 'market_value': money(value)})
    equity = signed_cash(account['cash']) + used + pnl + spot_value
    observed = [p.get('marked_at') for p in [*positions, *spot]]
    result = {**{k: v for k, v in account.items() if not k.startswith('_')}, 'spot': spot, 'positions': positions, 'currency': 'USDT', 'paper': True, 'loan_limit': settings(conn)['loan_limit'],
            'used_margin': money(used), 'unrealized_pnl': money(pnl), 'equity': money(equity),
            'net_equity': money(equity - number(account['debt'])),
            'available_cash': money(max(signed_cash(account['cash']) - number(account.get('reserved_cash', 0)), ZERO)),
            'execution_mode': 'paper', 'market_data_mode': 'live_public',
            'performance_pnl': money(equity - number(account['debt']) - Money(str(account['competition_capital']))) if account.get('competition_capital') is not None else None,
            'valuation_status': 'last_observed' if all(observed) else 'entry_prices',
            'oldest_mark_at': min(observed) if observed and all(observed) else None}
    from .paper_lifecycle import interest_reserve
    result['interest_reserve'] = interest_reserve(account, conn)
    if not project:
        return result
    from .paper_policy import account_facts, classify
    facts = account_facts(account, conn)
    excluded = dict(spot=0, positions=0, orders=0)
    unresolved = dict(spot=0, positions=0, orders=0)
    retained_margin, retained_cash = ZERO, ZERO
    reasons = set()
    if account.get('competition_capital') is None:
        reasons.add('paper_capital_unverified')
    for (kind, key), fact in facts.items():
        state = fact['classification']
        if state == 'binance':
            continue
        (excluded if state == 'excluded_venue' else unresolved)[kind] += 1
        reasons.add(state)
        if kind == 'positions':
            retained_margin += number(account[kind][key]['margin'])
    result['spot'] = [item for key, item in zip(account['spot'], spot) if facts[('spot', key)]['classification'] == 'binance']
    result['positions'] = [item for key, item in zip(account['positions'], positions) if facts[('positions', key)]['classification'] == 'binance']
    if conn is not None:
        from .paper_orders import rows
        for order in rows(conn, account['namespace'], account['user_id']):
            fact = classify(order, account=account, conn=conn)
            if fact['classification'] != 'binance':
                (excluded if fact['classification'] == 'excluded_venue' else unresolved)['orders'] += 1
                retained_cash += number(order.get('reserved_cash', 0))
                reasons.add(fact['classification'])
        from .paper_policy import performance_complete
        complete, reason = performance_complete(conn, account)
        if not complete:
            reasons.add(reason)
    result['filter'] = {'exchange': 'binance', 'excluded_resources': excluded, 'unresolved_resources': unresolved,
                        'retained_reserved_cash': money(retained_cash), 'retained_margin': money(retained_margin)}
    result['completeness'] = {'ledger': 'complete', 'valuation': 'incomplete' if reasons else 'complete',
                              'reason_codes': sorted(reasons)}
    if reasons:
        result.update(equity=None, net_equity=None, unrealized_pnl=None, performance_pnl=None, valuation_status='incomplete')
    return result


def get_account(caller, user_id):
    namespace = require(caller)
    identity(user_id)
    with store.connection() as conn:
        result = snapshot(_get(conn, namespace, user_id), conn=conn)
        conn.commit()  # Only additive policy/evidence projections were written.
        return result


def history(caller, user_id, limit=50):
    return history_page(caller, user_id, limit=limit)['items']


def history_page(caller, user_id, limit=50, offset=0, *, cursor=None, record_type='history', symbol=None, detail=False):
    namespace = require(caller)
    identity(user_id)
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError('Invalid history page')
    from .paper_storage import decode, history_limit, read_blob
    from .paper_policy import event_visible
    clauses, params = ['namespace=%s', 'user_id=%s'], [namespace, user_id]
    if cursor is not None:
        if type(cursor) is not int or cursor < 0:
            raise ValueError('Invalid history cursor')
        clauses.append('id<%s')
        params.append(cursor)
    count = min(limit, history_limit())
    with store.connection() as conn:
        if not conn.execute('SELECT 1 FROM paper_accounts WHERE namespace=%s AND user_id=%s', (namespace, user_id)).fetchone():
            raise ValueError('Paper account does not exist')
        metadata = conn.execute('SELECT id FROM paper_events WHERE ' +
                                ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT %s OFFSET %s', (*params, count + 1, offset)).fetchall()
        items, omitted = [], 0
        for row in metadata[:count]:
            event = dict(conn.execute('SELECT id,namespace,user_id,timestamp,kind,operation_id FROM paper_events WHERE id=%s', (row['id'],)).fetchone())
            event['payload'] = decode(read_blob(conn, 'paper_events', 'payload', row['id']), identity='paper history event')
            if not event_visible(event, conn=conn, account={'namespace': namespace, 'user_id': user_id}):
                omitted += 1
                continue
            payload = event['payload']
            if record_type == 'fills' and not payload.get('fill'):
                continue
            if symbol and payload.get('symbol') != symbol and (payload.get('fill') or {}).get('symbol') != symbol:
                continue
            items.append(event)
        conn.commit()
    scanned = min(len(metadata), count)
    return {'items': items, 'record_type': record_type, 'has_more': len(metadata) > count,
            'next_cursor': metadata[scanned - 1]['id'] if scanned else cursor,
            'next_offset': offset + scanned, 'filtered_count': omitted, 'scan_complete': len(metadata) <= count,
            'status': 'partial' if len(metadata) > count or omitted else 'complete', 'execution_mode': 'paper'}


def operation(caller, user_id, operation_id):
    namespace = require(caller)
    identity(user_id)
    with store.connection() as conn:
        from .paper_storage import one
        result = one(conn, 'paper_operations', 'result', 'namespace=%s AND user_id=%s AND operation_id=%s',
                     (namespace, user_id, operation_id), identity='original paper audit')
    if result is None:
        raise ValueError('Paper operation not found')
    return result


def bank(caller, user_id, action, amount, operation_id):
    amount = number(amount, positive=True)
    if amount != amount.quantize(Money('.00000001')):
        raise ValueError('Bank amounts support up to 8 decimal places')
    if action not in {'borrow', 'repay'}:
        raise ValueError('Bank action must be borrow or repay')

    def change(account, conn):
        cash, debt = signed_cash(account['cash']), number(account['debt'])
        if action == 'borrow':
            if not account['enabled']:
                raise ValueError('Paper account is disabled')
            if not settings(conn)['bank_enabled']:
                raise ValueError('Bank borrowing is disabled')
            view = snapshot(account, conn=conn)
            if debt > 0 and view['net_equity'] is not None and view['net_equity'] <= 0:
                raise ValueError('Cannot borrow additional funds when net equity is non-positive')
            if debt + amount > number(settings(conn)['loan_limit']):
                raise ValueError('Paper bank loan limit exceeded; an administrator can adjust it')
            cash, debt = cash + amount, debt + amount
        else:
            if amount > cash - number(account.get('reserved_cash', 0)) or amount > debt:
                raise ValueError('Repayment exceeds available cash or outstanding debt')
            cash, debt = cash - amount, debt - amount
        account.update(cash=money(cash), debt=money(debt))
        return {'bank_action': action, 'amount': money(amount), 'interest_rate': settings(conn)['daily_interest_rate']}

    return _write(caller, user_id, operation_id, {'action': action, 'amount': money(amount)}, change)


def set_leverage(caller, user_id, leverage, operation_id):
    if type(leverage) is not int or not 1 <= leverage <= settings()['max_leverage']:
        raise ValueError('Leverage exceeds the configured paper maximum')

    def change(account, conn):
        if not account['enabled']:
            raise ValueError('Paper account is disabled')
        # Leverage applies to subsequent entries; existing isolated margin stays fixed.
        account['leverage'] = leverage
        return {'leverage': leverage, 'applies_to': 'new_entries'}

    return _write(caller, user_id, operation_id, {'action': 'leverage', 'leverage': leverage}, change)


def market(symbol: str, market_type='swap', timeframe='1h', limit=120, *, candles=True, exchange_name=None, include=None) -> dict:
    from .paper_market import quote
    from .paper_policy import venue
    return quote(symbol, market_type, timeframe, limit, candles=candles, exchange_name=venue(exchange_name), include=include)


def _mark(account, quotes, conn, cfg):
    from .paper_policy import visible
    frozen = account.get('account_status') == 'bankrupt'
    for symbol, holding in account['spot'].items():
        if not visible(holding, account=account, kind='spot', key=symbol, conn=conn):
            continue
        quote = quotes.get(('spot', holding.get('symbol', symbol)))
        if quote and holding.get('exchange') and quote.get('exchange') != holding['exchange']:
            continue
        if quote:
            holding.update(mark_price=quote['price'], marked_at=quote['observed_at'])
    for key, p in list(account['positions'].items()):
        if not visible(p, account=account, kind='positions', key=key, conn=conn):
            continue
        quote = quotes.get(('swap', p['symbol']))
        if quote and p.get('exchange') and quote.get('exchange') != p['exchange']:
            continue
        if not quote:
            continue
        mark = number(quote['price'], positive=True)
        amount = number(p['amount'])
        pnl = (mark - number(p['entry_price'])) * amount * (1 if p['side'] == 'LONG' else -1)
        equity = number(p['margin']) + pnl
        maintenance = mark * amount * number(cfg['maintenance_margin_rate'])
        p.update(mark_price=money(mark), marked_at=quote['observed_at'])
        if equity <= maintenance and not frozen:
            # Isolated liquidation caps loss at posted margin; loans remain outstanding.
            fee = min(max(equity, ZERO), mark * amount * number(cfg['fee_rate']))
            payout = max(equity - fee, ZERO)
            account['cash'] = money(signed_cash(account['cash']) + payout)
            account['fees'] = money(number(account['fees']) + fee)
            realized = payout - number(p['margin'])
            account['realized_pnl'] = money(Money(str(account['realized_pnl'])) + realized)
            del account['positions'][key]
            _event(conn, account, 'liquidation', {'symbol': p['symbol'], 'exchange': p.get('exchange'), 'side': p['side'], 'amount': p['amount'],
                                               'origin_version': 1, 'resource_id': p.get('resource_id'), 'cycle_id': p.get('cycle_id'),
                                               'price': money(mark), 'realized_pnl': money(realized), 'fee': money(fee)})


def _fill(account, conn, quote, cfg, market_type, canonical, action, amount, leverage=None):
    from .paper_policy import stamp, venue
    venue(quote['exchange'])
    if action in {'buy', 'open_long', 'open_short'} and (not account['enabled'] or market_type not in account['markets']):
        raise ValueError('Paper account or requested market is disabled')
    if time.time() - quote['observed_at'] > 15:
        from .paper_orders import PaperQuoteExpired
        raise PaperQuoteExpired('Paper execution quote expired; no fill recorded')
    _mark(account, {(market_type, canonical): quote}, conn, cfg)
    from .paper_lifecycle import refresh_status, assert_active
    refresh_status(conn, account)
    assert_active(account)
    if action in {'buy', 'open_long', 'open_short'}:
        view = snapshot(account, conn=conn)
        if view['net_equity'] is not None and view['net_equity'] <= 0:
            raise ValueError('Cannot open new positions when net equity is non-positive; reduce debt or close positions first')
    buy = action in {'buy', 'open_long', 'close_short'}
    price = number(quote['ask'] if buy else quote['bid'], positive=True)
    notional = price * amount
    fee = notional * number(cfg['fee_rate'])
    if money(notional) <= 0:
        raise ValueError('Paper order is below minimum ledger precision')
    cash = signed_cash(account['cash'])
    available = cash - number(account.get('reserved_cash', 0))
    realized = ZERO
    resource_identity = {}
    if market_type == 'spot':
        from .paper_orders import exposure
        holding_key, holding = exposure(account, 'spot', canonical, None, quote['exchange'])
        holding_key = holding_key or (canonical if canonical not in account['spot'] else 'binance|' + canonical + '|' + uuid.uuid4().hex)
        old_amount = number(holding['amount']) if holding else ZERO
        if action == 'buy':
            if notional + fee > available:
                raise ValueError('Insufficient paper cash')
            cost = (number(holding['entry_price']) * old_amount if holding else ZERO) + notional
            account['spot'][holding_key] = {'symbol': canonical, 'exchange': quote['exchange'],
                                          'cycle_id': holding.get('cycle_id') if holding else uuid.uuid4().hex,
                                          'amount': float(old_amount + amount), 'entry_price': money(cost / (old_amount + amount)),
                                          'mark_price': quote['price'], 'marked_at': quote['observed_at']}
            stamp(account['spot'][holding_key], operation_id=(holding or {}).get('origin_operation_id'),
                  resource_id=(holding or {}).get('resource_id'))
            resource_identity = account['spot'][holding_key]
            cash -= notional + fee
        else:
            resource_identity = dict(holding or {})
            if amount > old_amount - number(account.get('reserved_spot', {}).get(holding_key, 0)):
                raise ValueError('Insufficient paper spot holdings')
            realized = (price - number(holding['entry_price'])) * amount - fee
            cash += notional - fee
            if amount == old_amount:
                del account['spot'][holding_key]
            else:
                holding['amount'] = float(old_amount - amount)
    else:
        side = 'LONG' if action.endswith('long') else 'SHORT'
        from .paper_orders import exposure
        key, p = exposure(account, 'swap', canonical, side, quote['exchange'])
        if key is None:
            candidate = 'binance|' + canonical + '|' + side
            key = candidate if candidate not in account['positions'] else candidate + '|' + uuid.uuid4().hex
        if action.startswith('open'):
            lev = leverage if leverage is not None else account['leverage']
            if lev > cfg['max_leverage']:
                raise ValueError('Account leverage exceeds the current paper maximum')
            if 1 / lev <= cfg['maintenance_margin_rate'] + cfg['fee_rate']:
                raise ValueError('Leverage leaves no maintenance margin; lower leverage or adjust paper rates')
            if p and p['leverage'] != lev:
                raise ValueError('Close this isolated position before reopening at different leverage')
            margin = notional / lev
            if margin + fee > available:
                raise ValueError('Insufficient paper margin')
            old_amount = number(p['amount']) if p else ZERO
            entry = ((number(p['entry_price']) * old_amount if p else ZERO) + notional) / (old_amount + amount)
            account['positions'][key] = {'symbol': canonical, 'exchange': quote['exchange'], 'side': side,
                                         'cycle_id': p.get('cycle_id') if p else uuid.uuid4().hex, 'amount': float(old_amount + amount),
                                         'entry_price': money(entry), 'margin': money((number(p['margin']) if p else ZERO) + margin),
                                         'added_margin': money(number(p.get('added_margin', 0))) if p else 0,
                                         'leverage': lev, 'mark_price': quote['price'], 'marked_at': quote['observed_at']}
            stamp(account['positions'][key], operation_id=(p or {}).get('origin_operation_id'),
                  resource_id=(p or {}).get('resource_id'))
            resource_identity = account['positions'][key]
            cash -= margin + fee
        else:
            resource_identity = dict(p or {})
            if not p or amount > number(p['amount']):
                raise ValueError('Paper close exceeds the open position (it may have liquidated)')
            old_amount = number(p['amount'])
            margin = number(p['margin']) * amount / old_amount
            pnl = (price - number(p['entry_price'])) * amount * (1 if side == 'LONG' else -1)
            payout = max(margin + pnl - fee, ZERO)
            realized = payout - margin
            fee = min(fee, max(margin + pnl, ZERO))
            cash += payout
            if amount == old_amount:
                del account['positions'][key]
            else:
                p.update(amount=float(old_amount - amount), margin=money(number(p['margin']) - margin),
                         added_margin=money(number(p.get('added_margin', 0)) * (old_amount - amount) / old_amount))
    account.update(cash=money(cash), fees=money(number(account['fees']) + fee),
                   realized_pnl=money(Money(str(account['realized_pnl'])) + realized))
    from .paper_orders import cleanup_orders
    cleanup_orders(conn, account)
    return {'fill': {'market_type': market_type, 'symbol': canonical, 'action': action, 'amount': float(amount),
                     'price': money(price), 'fee': money(fee), 'realized_pnl': money(realized),
                     'observed_at': quote['observed_at'], 'exchange': quote['exchange'], 'origin_version': 1,
                     'cycle_id': resource_identity.get('cycle_id'), 'resource_id': resource_identity.get('resource_id')}}


def trade(caller, user_id, market_type, symbol, action, amount, operation_id, leverage=None,
          exchange_name=None, stop_loss=None, take_profit=None):
    from .paper_orders import execute_market
    return execute_market(caller, user_id, market_type, symbol, action, operation_id,
                          amount=amount, leverage=leverage, exchange_name=exchange_name,
                          stop_loss=stop_loss, take_profit=take_profit)


def admin_accounts(namespace=None, user_id=None, limit=100, offset=0, *, cursor=None):
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError('Invalid pagination')
    clauses, params = [], []
    for column, value in [('namespace', namespace), ('user_id', user_id)]:
        if value:
            clauses.append(column + '=%s')
            params.append(identity(value, column))
    where = ' AND '.join(clauses) if clauses else '1=1'
    with store.connection() as conn:
        from .paper_storage import payload_page, PaperPayloadLimit
        count = conn.execute('SELECT COUNT(*) FROM paper_accounts WHERE ' + where, params).fetchone()[0]
        if cursor is None and offset:
            prior = conn.execute('SELECT storage_id AS rowid FROM paper_accounts WHERE ' + where + ' ORDER BY namespace,user_id LIMIT 1 OFFSET %s', [*params, offset - 1]).fetchone()
            if prior is None:
                # IDs reflect creation order, while this page is sorted by
                # namespace/user. An out-of-range offset must stay past that
                # ordering's last account, even when the newest ID sorts first.
                prior = conn.execute('SELECT storage_id FROM paper_accounts WHERE ' + where +
                                     ' ORDER BY namespace DESC,user_id DESC LIMIT 1', params).fetchone()
            cursor = prior[0] if prior else 0
        records, progress = payload_page(conn, 'paper_accounts', where, params, cursor=cursor or 0, count=limit, order_by=('namespace', 'user_id'))
        items = []
        for row in records:
            try:
                items.append(snapshot(upgrade_account(conn, row['payload']), conn=conn))
            except PaperPayloadLimit:
                progress['skipped_records'].append({'scan_id': row['scan_id'], 'reason_code': 'paper_payload_limit'})
                progress['status'] = 'partial'
        conn.commit()
    return {'items': items, 'total': count, 'limit': limit, 'offset': offset, **progress}


def admin_update(namespace, user_id, payload: AccountUpdate):
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    identity(namespace, 'namespace')
    identity(user_id)
    updates = payload.model_dump(exclude_none=True)
    for key in ('cash', 'debt', 'loan_limit'):
        if key in updates:
            updates[key] = money(updates[key])
    if updates.get('leverage', 1) > settings()['max_leverage']:
        raise ValueError('Leverage exceeds the paper maximum')
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', namespace, user_id)
        account = _get(conn, namespace, user_id)
        from .paper_lifecycle import settle, refresh_status
        settle(conn, account)
        if 'loan_limit' in updates:
            raise ValueError('Loan limit is configured centrally in bank settings')
        funding = Money(str(updates.get('cash', account['cash']))) - Money(str(account['cash']))
        funding -= Money(str(updates.get('debt', account['debt']))) - Money(str(account['debt']))
        if account['competition_capital'] is not None:
            account['competition_capital'] = money(Money(str(account['competition_capital'])) + funding)
        account.update(updates)
        refresh_status(conn, account)
        _save(conn, account)
        _event(conn, account, 'admin_adjustment', updates)
        conn.commit()
    with store.connection() as conn:
        result = snapshot(account, conn=conn)
        conn.commit()  # Persist this page of additive valuation/source proofs.
        return result


def maintenance_plan(*, max_items=None, max_accounts=None):
    """Stable cursor includes position within a large account's instrument set."""
    from .paper_storage import batch_size, decode, PaperPayloadLimit, read_blob
    from .paper_policy import visible
    from .paper_orders import rows
    item_limit = min(batch_size(), max_items if max_items is not None else batch_size())
    account_limit = min(batch_size(), max_accounts if max_accounts is not None else batch_size())
    if type(item_limit) is not int or type(account_limit) is not int or min(item_limit, account_limit) < 1:
        raise ValueError('Paper maintenance page limits must be positive integers')
    if not settings()['enabled']:
        return {'items': [], 'scan_complete': True, 'next_cursor': None}
    with store.connection() as conn:
        saved = conn.execute("SELECT cursor,payload FROM paper_scan_progress WHERE worker='maintenance'").fetchone()
        cursor, offset = (saved['cursor'], json.loads(saved['payload']).get('offset', 0)) if saved else (0, 0)
        metadata = conn.execute('SELECT storage_id AS rowid,namespace,user_id FROM paper_accounts WHERE storage_id>=%s ORDER BY storage_id LIMIT %s',
                                (cursor or 1, account_limit + 1)).fetchall()
        plan, scanned, skipped = [], 0, []
        for row in metadata[:account_limit]:
            try:
                account = upgrade_account(conn, decode(read_blob(conn, 'paper_accounts', 'payload', row['rowid'])))
                targets = {('spot' if kind == 'spot' else 'swap', item.get('symbol', key), 'binance')
                           for kind in ('spot', 'positions') for key, item in account[kind].items()
                           if visible(item, account=account, kind=kind, key=key, conn=conn)}
                targets |= {(order['market_type'], order['symbol'], 'binance') for order in rows(conn, account['namespace'], account['user_id'])
                            if visible(order, account=account, conn=conn)}
            except PaperPayloadLimit:
                skipped.append(row['rowid'])
                cursor, offset = row['rowid'] + 1, 0
                scanned += 1
                continue
            targets = sorted(targets)
            start = offset if row['rowid'] == cursor else 0
            selected = targets[start:start + item_limit - len(plan)]
            plan.extend({'namespace': row['namespace'], 'user_id': row['user_id'], 'market_type': item[0], 'symbol': item[1], 'exchange': 'binance'} for item in selected)
            consumed = start + len(selected)
            cursor, offset = (row['rowid'], consumed) if consumed < len(targets) else (row['rowid'] + 1, 0)
            scanned += 1
            if len(plan) >= item_limit:
                break
        last = conn.execute('SELECT COALESCE(MAX(storage_id),0) FROM paper_accounts').fetchone()[0]
        complete = cursor > last or not metadata
        next_cursor = None if complete else {'account_rowid': cursor, 'instrument_offset': offset}
        conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor,payload=excluded.payload',
                     ('maintenance', 0 if complete else cursor, json.dumps({'offset': 0 if complete else offset})))
        conn.commit()
    return {'items': plan, 'scan_complete': complete, 'next_cursor': next_cursor, 'skipped_records': skipped,
            'scanned_accounts': scanned, 'status': 'partial' if not complete or skipped else 'complete'}


def instruments():
    cfg = settings()
    if not cfg['enabled']:
        return []
    return sorted({(item['market_type'], item['symbol'], item['exchange']) for item in maintenance_plan()['items']})


def reconcile_instrument(market_type, symbol, exchange_name=None, *, namespace=None, user_id=None, cursor=0):
    from backend.utils.deployment_mode import ensure_execution_enabled
    ensure_execution_enabled()
    if not settings()['enabled']:
        return
    from .paper_orders import get_quote, match
    from .paper_policy import venue
    quote = get_quote(symbol, market_type, venue(exchange_name))
    cfg = settings()
    with store.connection() as conn:
        # Lock each selected paper account before reloading its state.
        from .paper_storage import payload_page
        where, params = ('namespace=%s AND user_id=%s', (namespace, user_id)) if namespace is not None else ('1=1', ())
        accounts, progress = payload_page(conn, 'paper_accounts', where, params, cursor=cursor)
        for record in sorted(accounts, key=lambda item: (item['namespace'], item['user_id'])):
            lock_scope(conn, 'paper-account', record['namespace'], record['user_id'])
        for record in accounts:
            if time.time() - quote['observed_at'] > 15:
                from .paper_orders import PaperQuoteExpired
                raise PaperQuoteExpired('Paper execution quote expired; wait for a fresh maintenance quote')
            account = _get(conn, record['namespace'], record['user_id'])
            from .paper_lifecycle import settle, refresh_status
            settle(conn, account)
            if time.time() - quote['observed_at'] > 15:
                from .paper_orders import PaperQuoteExpired
                raise PaperQuoteExpired('Paper execution quote expired; wait for a fresh maintenance quote')
            _mark(account, {(market_type, symbol): quote}, conn, cfg)
            refresh_status(conn, account)
            match(conn, account, market_type, symbol, quote, cfg)
            _save(conn, account)
        conn.commit()
    return progress


def reconcile():
    """Synchronous maintenance entry point for isolated validation and operations."""
    from .paper_storage import batch_size
    from .resilience import Budget, current_budget
    token = current_budget.set(Budget(None, uuid.uuid4().hex, 'execute'))
    from .paper_lifecycle import maintenance_finances
    maintenance_finances()
    scanned, processed = 0, 0
    try:
        while scanned < batch_size() and processed < batch_size():
            plan = maintenance_plan(max_items=1, max_accounts=batch_size() - scanned)
            scanned += plan.get('scanned_accounts', 0)
            if not plan['items']:
                break
            item = plan['items'][0]
            try:
                reconcile_instrument(item['market_type'], item['symbol'], item['exchange'], namespace=item['namespace'], user_id=item['user_id'])
            except Exception as exc:
                logger.warning('paper_mark_failed market=%s symbol=%s type=%s', item['market_type'], item['symbol'], type(exc).__name__)
            processed += 1
            if plan['scan_complete']:
                break
    finally:
        current_budget.reset(token)


async def maintenance_round(stop: asyncio.Event):
    """Every planned item advances durable progress; no unattempted page tail."""
    from .paper_storage import recover_receipts, batch_size
    from .paper_batch import recover_pending
    await run('recovery', recover_receipts, label='paper_receipt_updates')
    await run('recovery', recover_pending, label='paper_recovery')
    from .paper_lifecycle import maintenance_finances
    await run('execute', maintenance_finances, label='paper_daily_interest')
    scanned, processed = 0, 0
    while not stop.is_set() and scanned < batch_size() and processed < batch_size():
        plan = await run('read', partial(maintenance_plan, max_items=1, max_accounts=batch_size() - scanned),
                         label='paper_instruments')
        scanned += plan.get('scanned_accounts', 0)
        if not plan['items']:
            break
        item = plan['items'][0]
        try:
            await run('execute', partial(reconcile_instrument, item['market_type'], item['symbol'], item['exchange'],
                                         namespace=item['namespace'], user_id=item['user_id']),
                      label='paper_maintenance')
        except Exception as exc:
            logger.warning('paper_mark_failed market=%s symbol=%s type=%s', item['market_type'], item['symbol'], type(exc).__name__)
        processed += 1
        if plan['scan_complete']:
            break
    return {'processed_instruments': processed, 'scanned_accounts': scanned}


async def maintenance(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await maintenance_round(stop)
        except Exception as exc:
            logger.warning('paper_maintenance_failed type=%s', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass


from .paper_storage import bounded as _bounded
for _name in ('_write', 'create_account', 'get_account', 'history_page', 'admin_accounts', 'admin_update', 'maintenance_plan', 'reconcile_instrument'):
    globals()[_name] = _bounded(globals()[_name])
operation = _bounded(operation, critical=True)
