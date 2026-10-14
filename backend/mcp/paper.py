"""User-scoped paper ledger. No exchange credentials or private trading APIs."""
from __future__ import annotations

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


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    initial_balance: float = Field(10000, ge=0, le=1e12, allow_inf_nan=False)
    max_leverage: int = Field(125, ge=1, le=125)
    loan_limit: float = Field(1000000, ge=0, le=1e12, allow_inf_nan=False)
    fee_rate: float = Field(.001, ge=0, le=.01, allow_inf_nan=False)
    maintenance_margin_rate: float = Field(.005, gt=0, le=.1, allow_inf_nan=False)
    exchange: Literal['binance', 'okx', 'bitget', 'bybit'] = 'binance'
    bot_user_id: str = Field('bot:xiaojin-mengzhizi', pattern=r'^bot:[a-zA-Z0-9_.-]{1,80}$')
    bot_name: str = Field('小金萌智子', min_length=1, max_length=100)


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(None, max_length=100)
    cash: float | None = Field(None, ge=0, le=1e12, allow_inf_nan=False)
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


def settings() -> dict:
    return PaperSettings.model_validate(store.get('paper_settings', 'global') or {}).model_dump()


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


def money(value) -> float:
    result = Money(str(value))
    if not result.is_finite() or abs(result) > Money('1e15'):
        raise ValueError('Computed amount is outside the supported range')
    return float(result.quantize(Money('.00000001'), rounding=ROUND_HALF_UP))


def _get(conn, namespace, user_id):
    row = conn.execute('SELECT payload FROM paper_accounts WHERE namespace=? AND user_id=?', (namespace, user_id)).fetchone()
    if not row:
        raise ValueError('Paper account does not exist; call create_paper_account first')
    return upgrade_account(conn, json.loads(row[0]))


def _save(conn, account):
    account['updated_at'] = time.time()
    conn.execute('INSERT INTO paper_accounts VALUES(?,?,?) ON CONFLICT(namespace,user_id) DO UPDATE SET payload=excluded.payload',
                 (account['namespace'], account['user_id'], json.dumps(account, allow_nan=False)))


def upgrade_account(conn, account):
    if 'initial_capital' not in account:
        row = conn.execute('SELECT result FROM paper_operations WHERE namespace=? AND user_id=? ORDER BY rowid LIMIT 1',
                           (account['namespace'], account['user_id'])).fetchone()
        original = json.loads(row[0]).get('account', {}) if row else {}
        account['initial_capital'] = original.get('cash', settings()['initial_balance'])
        # Historical admin adjustments are funding, not trading performance.
        capital = Money(str(account['initial_capital']))
        old_cash, old_debt = account['initial_capital'], 0
        for event in conn.execute('SELECT kind,payload,operation_id FROM paper_events WHERE namespace=? AND user_id=? ORDER BY id', (account['namespace'], account['user_id'])):
            data = json.loads(event['payload'])
            if event['kind'] == 'admin_adjustment':
                capital += Money(str(data.get('cash', old_cash))) - Money(str(old_cash))
                capital -= Money(str(data.get('debt', old_debt))) - Money(str(old_debt))
            # New events contain cash/debt after each mutation for future audits.
            old_cash, old_debt = data.get('cash_after', data.get('cash', old_cash)), data.get('debt_after', data.get('debt', old_debt))
            if event['operation_id']:
                receipt = conn.execute('SELECT result FROM paper_operations WHERE namespace=? AND user_id=? AND operation_id=?',
                                       (account['namespace'], account['user_id'], event['operation_id'])).fetchone()
                if receipt:
                    recorded = json.loads(receipt[0]).get('account', {})
                    old_cash, old_debt = recorded.get('cash', old_cash), recorded.get('debt', old_debt)
        account['competition_capital'] = money(capital)
    account.setdefault('competition_capital', account['initial_capital'])
    account.setdefault('account_type', 'bot' if account['user_id'].startswith('bot:') else 'user')
    account.setdefault('leaderboard_visible', True)
    account.setdefault('notifications_enabled', True)
    account.setdefault('reserved_cash', 0)
    missing_source = any('exchange' not in p for p in [*account['spot'].values(), *account['positions'].values()])
    default_exchange = settings()['exchange'] if missing_source else None
    sources = {}
    if missing_source:
        for row in conn.execute('SELECT payload FROM paper_events WHERE namespace=? AND user_id=? ORDER BY id', (account['namespace'], account['user_id'])):
            fill = json.loads(row[0]).get('fill', {})
            if fill.get('exchange') and fill.get('symbol'):
                sources[(fill.get('market_type'), fill['symbol'])] = fill['exchange']
    for key, holding in account['spot'].items():
        holding.setdefault('symbol', key)
        holding.setdefault('exchange', sources.get(('spot', holding['symbol']), default_exchange))
        holding.setdefault('cycle_id', store.digest(account['namespace'] + ':' + account['user_id'] + ':' + key + ':' + str(account['created_at']))[:24])
    for position in account['positions'].values():
        position.setdefault('exchange', sources.get(('swap', position['symbol']), default_exchange))
        position.setdefault('cycle_id', store.digest(account['namespace'] + ':' + account['user_id'] + ':' + position['symbol'] + ':' + position['side'] + ':' + str(account['created_at']))[:24])
    return account


def _event(conn, account, kind, details, operation_id=None):
    conn.execute('INSERT INTO paper_events(namespace,user_id,timestamp,kind,operation_id,payload) VALUES(?,?,?,?,?,?)',
                 (account['namespace'], account['user_id'], time.time(), kind, operation_id,
                  json.dumps({**details, 'cash_after': account['cash'], 'debt_after': account['debt']}, allow_nan=False)))


def _receipt(conn, namespace, user_id, operation_id, request):
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
        raise ValueError('operation_id must be a stable 8–160 character identifier')
    fingerprint = store.digest(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False))
    row = conn.execute('SELECT fingerprint,result FROM paper_operations WHERE namespace=? AND user_id=? AND operation_id=?',
                       (namespace, user_id, operation_id)).fetchone()
    if row and row['fingerprint'] != fingerprint:
        raise ValueError('operation_id was already used for a different paper request')
    if row is None:
        parent = conn.execute('SELECT 1 FROM paper_batches WHERE namespace=? AND user_id=? AND operation_id=?',
                              (namespace, user_id, operation_id)).fetchone()
        if parent:
            raise ValueError('operation_id already belongs to a paper batch parent')
        reserved = conn.execute('SELECT parent_operation_id,action_index FROM paper_batch_children '
                                'WHERE namespace=? AND user_id=? AND child_operation_id=?',
                                (namespace, user_id, operation_id)).fetchone()
        if reserved:
            from .paper_batch import _step
            current = _step.get()
            if current is None or current[:4] != (namespace, user_id, reserved['parent_operation_id'], reserved['action_index']):
                raise ValueError('operation_id belongs to a reserved paper batch child')
    return fingerprint, json.loads(row['result']) if row else None


def _write(caller, user_id, operation_id, request, change):
    namespace = require(caller, 'trade')
    identity(user_id)
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        fingerprint, previous = _receipt(conn, namespace, user_id, operation_id, request)
        if previous is not None:
            return previous
        account = upgrade_account(conn, _get(conn, namespace, user_id))
        from .paper_orders import refresh_reservations
        refresh_reservations(conn, account)
        details = change(account, conn)
        refresh_reservations(conn, account)
        _save(conn, account)
        result = {'status': 'completed', 'paper': True, 'operation_id': operation_id,
                  'account': snapshot(account), **details}
        conn.execute('INSERT INTO paper_operations VALUES(?,?,?,?,?,?)',
                     (namespace, user_id, operation_id, fingerprint, caller['id'], json.dumps(result, allow_nan=False)))
        _event(conn, account, request['action'], details, operation_id)
        # A batch child and its parent progress share this account commit.
        from .paper_batch import record_child_commit
        record_child_commit(conn, result)
        conn.commit()
    return result


def create_account(caller, user_id: str, operation_id: str, name: str = '') -> dict:
    namespace = require(caller, 'trade')
    identity(user_id)
    if len(name) > 100:
        raise ValueError('Name is too long')
    request = {'action': 'create', 'name': name}
    cfg = settings()
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        fingerprint, previous = _receipt(conn, namespace, user_id, operation_id, request)
        if previous is not None:
            return previous
        row = conn.execute('SELECT payload FROM paper_accounts WHERE namespace=? AND user_id=?', (namespace, user_id)).fetchone()
        account = json.loads(row[0]) if row else {
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
        result = {'status': 'completed', 'paper': True, 'created': not bool(row), 'operation_id': operation_id, 'account': snapshot(account)}
        conn.execute('INSERT INTO paper_operations VALUES(?,?,?,?,?,?)',
                     (namespace, user_id, operation_id, fingerprint, caller['id'], json.dumps(result, allow_nan=False)))
        _event(conn, account, 'create', {'created': not bool(row)}, operation_id)
        conn.commit()
    return result


def snapshot(account: dict) -> dict:
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
    equity = number(account['cash']) + used + pnl + spot_value
    observed = [p.get('marked_at') for p in [*positions, *spot]]
    return {**account, 'spot': spot, 'positions': positions, 'currency': 'USDT', 'paper': True,
            'used_margin': money(used), 'unrealized_pnl': money(pnl), 'equity': money(equity),
            'net_equity': money(equity - number(account['debt'])),
            'available_cash': money(max(number(account['cash']) - number(account.get('reserved_cash', 0)), ZERO)),
            'execution_mode': 'paper', 'market_data_mode': 'live_public',
            'performance_pnl': money(equity - number(account['debt']) - Money(str(account.get('competition_capital', account.get('initial_capital', 0))))),
            'valuation_status': 'last_observed' if all(observed) else 'entry_prices',
            'oldest_mark_at': min(observed) if observed and all(observed) else None}


def get_account(caller, user_id):
    namespace = require(caller)
    identity(user_id)
    with store.connection() as conn:
        return snapshot(_get(conn, namespace, user_id))


def history(caller, user_id, limit=50):
    namespace = require(caller)
    identity(user_id)
    if isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError('limit must be 1–200')
    with store.connection() as conn:
        _get(conn, namespace, user_id)
        rows = conn.execute('SELECT * FROM paper_events WHERE namespace=? AND user_id=? ORDER BY id DESC LIMIT ?',
                            (namespace, user_id, limit)).fetchall()
    return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]


def operation(caller, user_id, operation_id):
    namespace = require(caller)
    identity(user_id)
    with store.connection() as conn:
        row = conn.execute('SELECT result FROM paper_operations WHERE namespace=? AND user_id=? AND operation_id=?',
                           (namespace, user_id, operation_id)).fetchone()
    if not row:
        raise ValueError('Paper operation not found')
    return json.loads(row[0])


def bank(caller, user_id, action, amount, operation_id):
    amount = number(amount, positive=True)
    if amount != amount.quantize(Money('.00000001')):
        raise ValueError('Bank amounts support up to 8 decimal places')
    if action not in {'borrow', 'repay'}:
        raise ValueError('Bank action must be borrow or repay')

    def change(account, conn):
        cash, debt = number(account['cash']), number(account['debt'])
        if action == 'borrow':
            if not account['enabled']:
                raise ValueError('Paper account is disabled')
            if debt + amount > number(account['loan_limit']):
                raise ValueError('Paper bank loan limit exceeded; an administrator can adjust it')
            cash, debt = cash + amount, debt + amount
        else:
            if amount > cash - number(account.get('reserved_cash', 0)) or amount > debt:
                raise ValueError('Repayment exceeds available cash or outstanding debt')
            cash, debt = cash - amount, debt - amount
        account.update(cash=money(cash), debt=money(debt))
        return {'bank_action': action, 'amount': money(amount), 'interest_rate': 0}

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


def market(symbol: str, market_type='swap', timeframe='1h', limit=120, *, candles=True, exchange_name=None) -> dict:
    from .paper_market import quote
    return quote(symbol, market_type, timeframe, limit, candles=candles, exchange_name=exchange_name)


def _mark(account, quotes, conn, cfg):
    for symbol, holding in account['spot'].items():
        quote = quotes.get(('spot', holding.get('symbol', symbol)))
        if quote and holding.get('exchange') and quote.get('exchange') != holding['exchange']:
            continue
        if quote:
            holding.update(mark_price=quote['price'], marked_at=quote['observed_at'])
    for key, p in list(account['positions'].items()):
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
        if equity <= maintenance:
            # Isolated liquidation caps loss at posted margin; loans remain outstanding.
            fee = min(max(equity, ZERO), mark * amount * number(cfg['fee_rate']))
            payout = max(equity - fee, ZERO)
            account['cash'] = money(number(account['cash']) + payout)
            account['fees'] = money(number(account['fees']) + fee)
            realized = payout - number(p['margin'])
            account['realized_pnl'] = money(Money(str(account['realized_pnl'])) + realized)
            del account['positions'][key]
            _event(conn, account, 'liquidation', {'symbol': p['symbol'], 'exchange': p.get('exchange'), 'side': p['side'], 'amount': p['amount'],
                                               'price': money(mark), 'realized_pnl': money(realized), 'fee': money(fee)})


def _fill(account, conn, quote, cfg, market_type, canonical, action, amount, leverage=None):
    if action in {'buy', 'open_long', 'open_short'} and (not account['enabled'] or market_type not in account['markets']):
        raise ValueError('Paper account or requested market is disabled')
    if time.time() - quote['observed_at'] > 15:
        raise ValueError('Paper execution quote expired; no fill recorded')
    _mark(account, {(market_type, canonical): quote}, conn, cfg)
    buy = action in {'buy', 'open_long', 'close_short'}
    price = number(quote['ask'] if buy else quote['bid'], positive=True)
    notional = price * amount
    fee = notional * number(cfg['fee_rate'])
    if money(notional) <= 0:
        raise ValueError('Paper order is below minimum ledger precision')
    cash = number(account['cash'])
    available = cash - number(account.get('reserved_cash', 0))
    realized = ZERO
    if market_type == 'spot':
        from .paper_orders import exposure
        holding_key, holding = exposure(account, 'spot', canonical, None, quote['exchange'])
        holding_key = holding_key or (canonical if canonical not in account['spot'] else quote['exchange'] + '|' + canonical)
        old_amount = number(holding['amount']) if holding else ZERO
        if action == 'buy':
            if notional + fee > available:
                raise ValueError('Insufficient paper cash')
            cost = (number(holding['entry_price']) * old_amount if holding else ZERO) + notional
            account['spot'][holding_key] = {'symbol': canonical, 'exchange': quote['exchange'],
                                          'cycle_id': holding.get('cycle_id') if holding else uuid.uuid4().hex,
                                          'amount': float(old_amount + amount), 'entry_price': money(cost / (old_amount + amount)),
                                          'mark_price': quote['price'], 'marked_at': quote['observed_at']}
            cash -= notional + fee
        else:
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
        key = key or quote['exchange'] + '|' + canonical + '|' + side
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
                                         'leverage': lev, 'mark_price': quote['price'], 'marked_at': quote['observed_at']}
            cash -= margin + fee
        else:
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
                p.update(amount=float(old_amount - amount), margin=money(number(p['margin']) - margin))
    account.update(cash=money(cash), fees=money(number(account['fees']) + fee),
                   realized_pnl=money(Money(str(account['realized_pnl'])) + realized))
    from .paper_orders import cleanup_orders
    cleanup_orders(conn, account)
    return {'fill': {'market_type': market_type, 'symbol': canonical, 'action': action, 'amount': float(amount),
                     'price': money(price), 'fee': money(fee), 'realized_pnl': money(realized),
                     'observed_at': quote['observed_at'], 'exchange': quote['exchange']}}


def trade(caller, user_id, market_type, symbol, action, amount, operation_id, leverage=None,
          exchange_name=None, stop_loss=None, take_profit=None):
    from .paper_orders import execute_market
    return execute_market(caller, user_id, market_type, symbol, action, operation_id,
                          amount=amount, leverage=leverage, exchange_name=exchange_name,
                          stop_loss=stop_loss, take_profit=take_profit)


def admin_accounts(namespace=None, user_id=None, limit=100, offset=0):
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError('Invalid pagination')
    clauses, params = [], []
    for column, value in [('namespace', namespace), ('user_id', user_id)]:
        if value:
            clauses.append(column + '=?')
            params.append(identity(value, column))
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with store.connection() as conn:
        count = conn.execute('SELECT COUNT(*) FROM paper_accounts' + where, params).fetchone()[0]
        rows = conn.execute('SELECT payload FROM paper_accounts' + where + ' ORDER BY namespace,user_id LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
    return {'items': [snapshot(json.loads(r[0])) for r in rows], 'total': count, 'limit': limit, 'offset': offset}


def admin_update(namespace, user_id, payload: AccountUpdate):
    identity(namespace, 'namespace')
    identity(user_id)
    updates = payload.model_dump(exclude_none=True)
    for key in ('cash', 'debt', 'loan_limit'):
        if key in updates:
            updates[key] = money(updates[key])
    if updates.get('leverage', 1) > settings()['max_leverage']:
        raise ValueError('Leverage exceeds the paper maximum')
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        account = _get(conn, namespace, user_id)
        funding = Money(str(updates.get('cash', account['cash']))) - Money(str(account['cash']))
        funding -= Money(str(updates.get('debt', account['debt']))) - Money(str(account['debt']))
        account['competition_capital'] = money(Money(str(account['competition_capital'])) + funding)
        account.update(updates)
        _save(conn, account)
        _event(conn, account, 'admin_adjustment', updates)
        conn.commit()
    return snapshot(account)


def instruments():
    cfg = settings()
    if not cfg['enabled']:
        return []
    with store.connection() as conn:
        accounts = [json.loads(r[0]) for r in conn.execute('SELECT payload FROM paper_accounts')]
    result = {('spot', h.get('symbol', symbol), h.get('exchange', cfg['exchange'])) for a in accounts for symbol, h in a['spot'].items()}
    result |= {('swap', p['symbol'], p.get('exchange', cfg['exchange'])) for a in accounts for p in a['positions'].values()}
    with store.connection() as conn:
        for row in conn.execute("SELECT payload FROM paper_orders WHERE state='open'"):
            order = json.loads(row[0])
            result.add((order['market_type'], order['symbol'], order['exchange']))
    return sorted(result)


def reconcile_instrument(market_type, symbol, exchange_name=None):
    if not settings()['enabled']:
        return
    from .paper_orders import get_quote, match
    quote = get_quote(symbol, market_type, exchange_name)
    cfg = settings()
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        accounts = [upgrade_account(conn, json.loads(r[0])) for r in conn.execute('SELECT payload FROM paper_accounts')]
        for account in accounts:
            _mark(account, {(market_type, symbol): quote}, conn, cfg)
            match(conn, account, market_type, symbol, quote, cfg)
            _save(conn, account)
        conn.commit()


def reconcile():
    """Synchronous maintenance entry point for isolated validation and operations."""
    for market_type, symbol, exchange_name in instruments():
        try:
            reconcile_instrument(market_type, symbol, exchange_name)
        except Exception as exc:
            logger.warning('paper_mark_failed market=%s symbol=%s type=%s', market_type, symbol, type(exc).__name__)


async def maintenance(stop: asyncio.Event):
    while not stop.is_set():
        try:
            symbols = await run('read', instruments, label='paper_instruments')
            for market_type, symbol, exchange_name in symbols:
                if stop.is_set():
                    break
                try:
                    # Each instrument has a fresh bounded budget, so an outage on
                    # one market cannot starve liquidation checks on later ones.
                    await run('execute', partial(reconcile_instrument, market_type, symbol, exchange_name), label='paper_maintenance')
                except Exception as exc:
                    logger.warning('paper_mark_failed market=%s symbol=%s type=%s', market_type, symbol, type(exc).__name__)
        except Exception as exc:
            logger.warning('paper_maintenance_failed type=%s', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass
