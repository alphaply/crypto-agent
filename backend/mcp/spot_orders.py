"""MCP-owned spot inventory and exchange-native, quantity-reserved exits.

No local price watcher emulates stops. Unacknowledged submissions retain their
reservation and are recovered only by exact exchange/client order identity.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Literal

import ccxt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend import database
from backend.utils.execution_ledger import account_scope
from backend.utils.spot_config_guard import serialized_spot_execution


class SpotExitRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: float = Field(gt=0, allow_inf_nan=False)
    exit_type: Literal['market', 'limit', 'stop_loss', 'take_profit', 'oco']
    price: float | None = Field(None, gt=0, allow_inf_nan=False)
    stop_loss: float | None = Field(None, gt=0, allow_inf_nan=False)
    take_profit: float | None = Field(None, gt=0, allow_inf_nan=False)
    stop_limit_price: float | None = Field(None, gt=0, allow_inf_nan=False)
    take_profit_limit_price: float | None = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def validate_prices(self):
        expected = {'market': set(), 'limit': {'price'}, 'stop_loss': {'stop_loss', 'stop_limit_price'},
                    'take_profit': {'take_profit', 'take_profit_limit_price'},
                    'oco': {'stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price'}}[self.exit_type]
        required = {'market': set(), 'limit': {'price'}, 'stop_loss': {'stop_loss'},
                    'take_profit': {'take_profit'}, 'oco': {'stop_loss', 'take_profit'}}[self.exit_type]
        for name in ('price', 'stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price'):
            if name not in expected and getattr(self, name) is not None:
                raise ValueError(f'{name} is not used by this exit type')
            if name in required and getattr(self, name) is None:
                raise ValueError(f'{name} is required')
        if self.stop_limit_price and self.stop_limit_price > self.stop_loss:
            raise ValueError('Sell stop limit price must not exceed its trigger')
        if self.take_profit_limit_price and self.take_profit_limit_price > self.take_profit:
            raise ValueError('Sell take-profit limit price must not exceed its trigger')
        if self.stop_loss and self.take_profit and self.stop_loss >= self.take_profit:
            raise ValueError('stop_loss must be below take_profit')
        return self


class SpotExitCancelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    exit_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


TOOL_MODELS = {'create_spot_exit': SpotExitRequest, 'cancel_spot_exit': SpotExitCancelRequest}
_TERMINAL = {'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
_EXIT_TERMINAL = {'filled', 'cancelled', 'failed'}


def validate_arguments(tool_name, arguments):
    if tool_name not in TOOL_MODELS:
        raise ValueError('Unknown spot exit tool')
    return TOOL_MODELS[tool_name].model_validate(arguments).model_dump(exclude_none=True)


def tool_schemas():
    descriptions = {
        'create_spot_exit': 'Sell only verified MCP-owned spot inventory: market/limit exit, exchange-native stop loss, take profit, or OCO. OCO shares one amount between mutually exclusive legs. Query inventory first. Mixed/manual holdings or unknown orders block selling. Submitted is not filled. Limit triggers need explicit stop_limit_price/take_profit_limit_price when required by exchange capabilities.',
        'cancel_spot_exit': 'Cancel one exit or native OCO owned by this MCP profile, using its exit_id. Does not cancel other orders. Remaining fills must be verified before releasing quantity.',
    }
    return [{'name': name, 'description': descriptions[name], 'inputSchema': model.model_json_schema()}
            for name, model in TOOL_MODELS.items()]


def _initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS mcp_spot_exits (
        exit_id TEXT PRIMARY KEY, config_id TEXT NOT NULL, symbol TEXT NOT NULL,
        account_scope TEXT NOT NULL, operation_id TEXT NOT NULL, request_hash TEXT NOT NULL,
        kind TEXT NOT NULL, amount REAL NOT NULL, filled REAL NOT NULL DEFAULT 0,
        state TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(config_id,operation_id))''')


def _rows(config_id, symbol=None):
    with database.get_db_conn() as conn:
        _initialize(conn)
        query = 'SELECT * FROM mcp_spot_exits WHERE config_id=?'
        args = [config_id]
        if symbol:
            query += ' AND symbol=?'
            args.append(symbol)
        rows = [dict(row) for row in conn.execute(query, args)]
        conn.commit()
    return [{**row, 'payload': json.loads(row['payload'])} for row in rows]


def _save(row):
    with database.get_db_conn() as conn:
        conn.execute('UPDATE mcp_spot_exits SET filled=?,state=?,payload=?,updated_at=? WHERE exit_id=?',
                     (row['filled'], row['state'], json.dumps(row['payload']), time.time(), row['exit_id']))
        conn.commit()


def _number(value, label):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'Cannot verify {label}') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f'Invalid {label}')
    return result


def _tool(config_id, symbol):
    from backend.config import config
    from backend.utils.market_data import MarketTool
    cfg = config.get_config_by_id(config_id)
    if not str(config_id).startswith('mcp:') or not cfg or cfg.get('market_type') != 'spot':
        raise ValueError('Spot exits require an MCP spot profile')
    mt = MarketTool(config_id=config_id)
    market = mt.exchange.market(symbol)
    if not market.get('spot') or market.get('contract'):
        raise ValueError('Spot exits require a spot exchange market')
    return mt, market


def _okx_row(response):
    data = response.get('data') or []
    if str(response.get('code', '0')) != '0':
        raise ccxt.InvalidOrder('OKX rejected the spot request with code ' + str(response.get('code')))
    if len(data) != 1:
        raise ValueError('OKX did not return one verified order result')
    row = data[0]
    if str(row.get('sCode', '0')) != '0':
        raise ccxt.InvalidOrder('OKX rejected the spot exit: ' + str(row.get('sMsg') or row['sCode']))
    return row


def _order(exchange, order_id, symbol, side):
    order = exchange.fetch_order(str(order_id), symbol)
    if str(order.get('id')) != str(order_id) or order.get('symbol') != symbol or order.get('side') != side:
        raise ValueError('Exchange order identity does not match owned spot evidence')
    _number(order.get('filled'), 'filled quantity')
    if str(order.get('status') or '').lower() not in _TERMINAL | {'open'}:
        raise ValueError('Exchange order status is unresolved')
    return order


def _refresh(exchange, row):
    """Resolve native group children, then verify actual ordinary order fills."""
    p = row['payload']
    if row['state'] == 'failed':
        return []
    symbol = row['symbol']
    market = exchange.market(symbol)
    native_done = False
    if p['adapter'] == 'binance_oco':
        query = {'orderListId': p['exchange_id']} if p.get('exchange_id') else {'origClientOrderId': p['client_id']}
        native = exchange.privateGetOrderList(query)
        if str(native.get('listClientOrderId')) != p['client_id'] or native.get('symbol') != market['id']:
            raise ValueError('OCO identity does not match this exit')
        p['exchange_id'] = str(native['orderListId'])
        p['order_ids'] = list(dict.fromkeys(str(item['orderId']) for item in native.get('orders') or []))
        if len(p['order_ids']) != 2:
            raise ValueError('Cannot verify both native OCO legs')
        native_done = native.get('listOrderStatus') == 'ALL_DONE'
    elif p['adapter'] == 'okx_algo':
        query = {'algoId': p['exchange_id']} if p.get('exchange_id') else {'algoClOrdId': p['client_id']}
        native = _okx_row(exchange.privateGetTradeOrderAlgo(query))
        if native.get('algoClOrdId') != p['client_id'] or native.get('instId') != market['id'] or native.get('side') != 'sell':
            raise ValueError('Algo identity does not match this exit')
        native_amount = _number(native.get('sz'), 'native algo quantity')
        if native_amount <= 0 or native_amount > row['amount'] + 1e-10:
            raise ValueError('Native algo quantity exceeds or invalidates its original reservation')
        p['exchange_id'] = str(native['algoId'])
        p['order_ids'] = list(dict.fromkeys(str(value) for value in [*(native.get('ordIdList') or []), native.get('ordId')] if value))
        native_done = native.get('state') in {'canceled', 'order_failed', 'effective'}
        if native.get('state') in {'effective', 'partially_effective'} and not p['order_ids']:
            raise ValueError('Triggered algo has no verifiable child order')
        if not p['order_ids']:
            # A live algo may omit execution size. A terminal algo can release
            # its reservation only with explicit evidence of zero execution.
            actual_size = native.get('actualSz')
            if native_done or actual_size not in (None, ''):
                if _number(actual_size, 'algo execution size') > 0:
                    raise ValueError('Algo execution has no verifiable child order')
        if native.get('state') not in {'live', 'pause', 'partially_effective', 'effective', 'canceled', 'order_failed'}:
            raise ValueError('Algo order status is unresolved')
    else:
        if not p.get('exchange_id'):
            found = exchange.fetch_order(p['client_id'], symbol, {'clientOrderId': p['client_id']})
            if found.get('clientOrderId') != p['client_id'] or not found.get('id'):
                raise ValueError('Unacknowledged exit cannot be identified')
            p['exchange_id'] = str(found['id'])
        p['order_ids'] = [p['exchange_id']]
        native_done = True
    orders = [_order(exchange, order_id, symbol, 'sell') for order_id in p.get('order_ids', [])]
    if any(_number(order.get('amount'), 'exit quantity') > row['amount'] + 1e-10 for order in orders):
        raise ValueError('Exchange exit quantity exceeds its original reservation')
    filled = sum(_number(order.get('filled'), 'exit fills') for order in orders)
    if filled > row['amount'] + max(1e-10, row['amount'] * 1e-8):
        raise ValueError('Exit fills exceed its reserved quantity')
    if filled + 1e-10 < row['filled']:
        raise ValueError('Exchange fill quantity regressed')
    row['filled'] = filled
    done = native_done and all(str(order['status']).lower() in _TERMINAL for order in orders)
    row['state'] = ('filled' if filled >= row['amount'] - 1e-10 else 'cancelled') if done else 'open'
    p.pop('error', None)
    _save(row)
    return orders


def _fees_and_fills(exchange, symbol, orders, since):
    """Use complete matching trade evidence; fetch_order alone omits base fees."""
    trades = exchange.fetch_my_trades(symbol, since=since, limit=1000)
    if len(trades) >= 1000:
        raise ValueError('Spot fill history exceeds one verifiable page; reconcile before selling')
    seen = set()
    owned = {str(order['id']): order for order in orders}
    amounts = {key: 0.0 for key in owned}
    base_fees = {key: 0.0 for key in owned}
    base = symbol.split('/')[0]
    for trade in trades:
        if trade.get('id') is None or trade.get('order') is None:
            raise ValueError('Spot fills have no stable ownership identity')
        if str(trade['id']) in seen:
            continue
        seen.add(str(trade['id']))
        order_id = str(trade['order'])
        if order_id not in owned:
            raise ValueError('Manual or another strategy\'s fills share this spot asset; selling denied')
        if trade.get('side') != owned[order_id]['side']:
            raise ValueError('Spot fill direction conflicts with owned order')
        amounts[order_id] += _number(trade.get('amount'), 'trade quantity')
        fees = trade.get('fees') or ([trade['fee']] if trade.get('fee') else [])
        if not fees:
            raise ValueError('Spot fill fees are unavailable; net owned quantity cannot be verified')
        for fee in fees:
            cost = _number(fee.get('cost'), 'trade fee')
            if not fee.get('currency'):
                raise ValueError('Spot fee currency is unavailable')
            if fee['currency'] == base:
                base_fees[order_id] += cost
    quantity = 0.0
    for order_id, order in owned.items():
        filled = _number(order.get('filled'), 'order fill')
        if not math.isclose(amounts[order_id], filled, rel_tol=1e-8, abs_tol=1e-10):
            raise ValueError('Spot fill history is incomplete')
        quantity += (filled - base_fees[order_id]) if order['side'] == 'buy' else -(filled + base_fees[order_id])
    return quantity


def _configured_account_scope(config_id):
    from backend.config import config
    if not config_id or not config.get_config_by_id(config_id):
        return None
    exchange_name, api_key, _, _ = config.get_exchange_credentials(config_id=config_id)
    if not api_key:
        return None
    return account_scope(SimpleNamespace(id=exchange_name, apiKey=api_key), config_id)


def _assert_no_foreign_ownership(conn, config_id, base, scope):
    """Balance equality alone cannot rule out another strategy's same-base lot."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    evidence = list(conn.execute('''SELECT DISTINCT config_id,symbol,order_id FROM orders
        WHERE COALESCE(config_id,'')<>? AND trade_mode='SPOT_DCA'
        AND UPPER(side) IN ('BUY','BUY_LIMIT')
        AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' ''', (config_id,)))
    if 'spot_budget_reservations' in tables:
        evidence.extend(conn.execute('''SELECT config_id,symbol,order_id FROM spot_budget_reservations
            WHERE config_id<>? AND status<>'released' ''', (config_id,)))
    owners = {}
    for row in evidence:
        if str(row['symbol']).split('/')[0] != base:
            continue
        if 'spot_order_fills' in tables and conn.execute('''SELECT 1 FROM spot_order_fills
            WHERE config_id=? AND symbol=? AND order_id=?
            AND UPPER(status) IN ('CANCELLED','CANCELED','EXPIRED','REJECTED')
            AND COALESCE(filled_qty,0)=0 AND COALESCE(filled_cost,0)=0''',
                (row['config_id'], row['symbol'], row['order_id'])).fetchone():
            continue
        owner = row['config_id']
        if owner not in owners:
            owners[owner] = _configured_account_scope(owner)
        if owners[owner] is None or owners[owner] == scope:
            raise ValueError('Another strategy has same-asset spot ownership or unresolved buy evidence; exclusive inventory cannot be established')


def _inventory(mt, symbol):
    exchange = mt.exchange
    config_id = mt.config_id
    base = symbol.split('/')[0]
    scope = account_scope(exchange, config_id)
    exits = [row for row in _rows(config_id) if row['symbol'].split('/')[0] == base]
    with database.get_db_conn() as conn:
        _assert_no_foreign_ownership(conn, config_id, base, scope)
        buys = [dict(row) for row in conn.execute('''SELECT order_id,symbol,MIN(timestamp) AS timestamp FROM orders
            WHERE config_id=? AND trade_mode='SPOT_DCA' AND UPPER(side) IN ('BUY','BUY_LIMIT')
            AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' GROUP BY symbol,order_id''', (config_id,))
            if row['symbol'].split('/')[0] == base]
        foreign_exits = conn.execute('SELECT 1 FROM mcp_spot_exits WHERE account_scope=? AND config_id<>? AND state NOT IN (\'filled\',\'cancelled\',\'failed\') AND symbol LIKE ? LIMIT 1',
                                     (scope, config_id, base + '/%')).fetchone()
    if foreign_exits:
        raise ValueError('Another MCP profile has reserved this spot asset')
    if not buys:
        raise ValueError('No MCP-owned spot buys can establish inventory ownership')
    by_symbol = {}
    starts = []
    for buy in buys:
        order = _order(exchange, buy['order_id'], buy['symbol'], 'buy')
        by_symbol.setdefault(buy['symbol'], []).append(order)
        stamp = datetime.fromisoformat(buy['timestamp'])
        if stamp.tzinfo is None:
            stamp = database.TZ_CN.localize(stamp)
        starts.append(int(stamp.timestamp() * 1000) - 60000)
    reserved = 0.0
    for row in exits:
        if row['account_scope'] != scope:
            raise ValueError('Spot exit account has changed; ownership cannot be verified')
        try:
            orders = _refresh(exchange, row)
        except Exception:
            row['state'] = 'unknown'
            _save(row)
            raise ValueError('A spot exit is unresolved; reserved quantity cannot be reused') from None
        by_symbol.setdefault(row['symbol'], []).extend(orders)
        if row['state'] not in _EXIT_TERMINAL:
            reserved += max(0, row['amount'] - row['filled'])
    owned = sum(_fees_and_fills(exchange, asset_symbol, orders, min(starts))
                for asset_symbol, orders in by_symbol.items())
    balance = exchange.fetch_balance()
    asset = balance.get(base) or {}
    total = _number(asset.get('total', (balance.get('total') or {}).get(base)), 'spot total balance')
    free = _number(asset.get('free', (balance.get('free') or {}).get(base)), 'spot free balance')
    used = _number(asset.get('used', (balance.get('used') or {}).get(base)), 'spot locked balance')
    tolerance = max(1e-10, abs(owned) * 1e-8)
    if owned < -tolerance or not math.isclose(total, owned, rel_tol=1e-8, abs_tol=1e-10):
        raise ValueError('Account base balance differs from verified MCP-owned inventory; manual/shared holdings or transfers need reconciliation')
    if free > total + tolerance or not math.isclose(free + used, total, rel_tol=1e-8, abs_tol=1e-10) or used > reserved + tolerance:
        raise ValueError('Unexplained spot balance locks prevent safe selling')
    return {'symbol': symbol, 'base_asset': base, 'owned_quantity': max(0, owned),
            'reserved_quantity': reserved, 'available_quantity': max(0, min(free, owned - reserved)),
            'exchange_free': free, 'exchange_locked': used, 'account_scope': scope,
            '_exit_filled_quantity': sum(row['filled'] for row in exits),
            'exits': [_receipt(row) for row in exits]}


def _receipt(row):
    result = {'exit_id': row['exit_id'], 'symbol': row['symbol'], 'exit_type': row['kind'], 'status': row['state'],
            'amount': row['amount'], 'filled': row['filled'], 'exchange_id': row['payload'].get('exchange_id'),
            'order_ids': row['payload'].get('order_ids', []),
            'message': 'Exchange acknowledgement is not fill confirmation.'}
    if row['payload'].get('error'):
        result['error'] = row['payload']['error']
        if row['state'] == 'failed':
            result['failure_reason'] = {
                'InvalidOrder': 'Exchange rejected the order parameters or instrument limits. Check supported exit types, prices, quantity, and exchange limits before creating a different operation.',
                'InsufficientFunds': 'Exchange rejected the order for insufficient available balance. Reconcile balances and existing reservations before creating a different operation.',
                'AuthenticationError': 'Exchange rejected account authentication. Correct the account credentials before creating a different operation.',
                'PermissionDenied': 'Exchange denied trading permission for this account or instrument. Correct account permissions before creating a different operation.',
                'NotSupported': 'Exchange does not support this order type or parameter combination. Query the instrument exit capabilities before creating a different operation.',
            }.get(result['error'], 'Exchange rejected the order. Verify the account and order constraints before creating a different operation.')
        else:
            result['failure_reason'] = 'Exchange outcome is unknown and quantity remains reserved. Do not resubmit; query spot inventory to reconcile the existing exit.'
        result['automatic_retry_allowed'] = False
    return result


@serialized_spot_execution
def inventory(config_id, symbol):
    mt, market = _tool(config_id, symbol)
    result = _inventory(mt, market['symbol'])
    result.pop('account_scope', None)
    result.pop('_exit_filled_quantity', None)
    result['capabilities'] = capabilities(mt.exchange, market)
    return result


def capabilities(exchange, market):
    types = set((market.get('info') or {}).get('orderTypes') or [])
    rules = {'native_only': True, 'oco_reservation': 'One amount shared by exchange-native mutually exclusive legs',
             'stop_limit_price': 'Required when stop_loss_market is false; optional otherwise',
             'take_profit_limit_price': 'Required when take_profit_market is false; optional otherwise',
             'trigger_directions': 'For sells: stop_loss < current price < take_profit',
             'inventory_policy': 'Only verified exclusive MCP-owned inventory; shared/manual holdings and unknown exits block selling'}
    if exchange.id == 'binance':
        return {**rules, 'market': 'MARKET' in types, 'limit': 'LIMIT' in types,
                'stop_loss_market': 'STOP_LOSS' in types, 'stop_loss_limit': 'STOP_LOSS_LIMIT' in types,
                'take_profit_market': 'TAKE_PROFIT' in types, 'take_profit_limit': 'TAKE_PROFIT_LIMIT' in types,
                'oco': bool((market.get('info') or {}).get('ocoAllowed'))}
    if exchange.id == 'okx':
        return {**rules, 'market': True, 'limit': True, 'stop_loss_market': True, 'stop_loss_limit': True,
                'take_profit_market': True, 'take_profit_limit': True, 'oco': True,
                'account_mode': 'cash', 'note': 'Exchange/account eligibility and instrument restrictions still apply'}
    return {**rules, 'supported': False}


def _submission(exchange, market, args, client_id):
    """Return an exact native request before quantity reservation or writes."""
    symbol, kind = market['symbol'], args['exit_type']
    amount = float(exchange.amount_to_precision(symbol, args['amount']))
    if not math.isfinite(amount) or amount <= 0 or amount > args['amount'] + 1e-12:
        raise ValueError('Invalid precision-adjusted sell quantity')
    limits = (market.get('limits') or {}).get('amount') or {}
    if (limits.get('min') is not None and amount < float(limits['min'])) or (limits.get('max') is not None and amount > float(limits['max'])):
        raise ValueError('Sell quantity is outside the exchange instrument limits')
    price = lambda value: exchange.price_to_precision(symbol, value)
    if kind in {'stop_loss', 'take_profit', 'oco'}:
        last = _number(exchange.fetch_ticker(symbol).get('last'), 'current market price')
        if args.get('stop_loss') and args['stop_loss'] >= last:
            raise ValueError('Sell stop-loss trigger must be below the current price')
        if args.get('take_profit') and args['take_profit'] <= last:
            raise ValueError('Sell take-profit trigger must be above the current price')
    if exchange.id not in {'binance', 'okx'}:
        raise ValueError('Native spot exits are supported only on Binance and OKX')
    if exchange.id == 'okx' and kind not in {'market', 'limit'}:
        request = {'instId': market['id'], 'tdMode': 'cash', 'side': 'sell',
                   'ordType': 'oco' if kind == 'oco' else 'conditional', 'sz': str(amount), 'algoClOrdId': client_id}
        for key, prefix, execution in (('stop_loss', 'sl', 'stop_limit_price'), ('take_profit', 'tp', 'take_profit_limit_price')):
            if args.get(key):
                request[prefix + 'TriggerPx'] = price(args[key])
                request[prefix + 'OrdPx'] = price(args[execution]) if args.get(execution) else '-1'
                request[prefix + 'TriggerPxType'] = 'last'
        return amount, 'okx_algo', request
    supported = set((market.get('info') or {}).get('orderTypes') or [])
    if exchange.id == 'binance' and kind == 'oco':
        if not (market.get('info') or {}).get('ocoAllowed'):
            raise ValueError('This Binance market does not support native OCO')
        request = {'symbol': market['id'], 'side': 'SELL', 'quantity': str(amount), 'listClientOrderId': client_id,
                   'aboveClientOrderId': client_id + 'T', 'belowClientOrderId': client_id + 'S'}
        for prefix, trigger, limit_key, market_type in (
            ('above', 'take_profit', 'take_profit_limit_price', 'TAKE_PROFIT'),
            ('below', 'stop_loss', 'stop_limit_price', 'STOP_LOSS'),
        ):
            order_type = market_type + '_LIMIT' if args.get(limit_key) else market_type
            if order_type not in supported:
                raise ValueError(f'Native {order_type} is unsupported; provide {limit_key} for a supported limit trigger')
            request[prefix + 'Type'] = order_type
            request[prefix + 'StopPrice'] = price(args[trigger])
            if args.get(limit_key):
                request[prefix + 'Price'] = price(args[limit_key])
                request[prefix + 'TimeInForce'] = 'GTC'
        return amount, 'binance_oco', request
    order_type = kind.upper()
    limit_price = args.get('price')
    params = {'clientOrderId': client_id}
    if kind in {'stop_loss', 'take_profit'}:
        limit_price = args.get('stop_limit_price' if kind == 'stop_loss' else 'take_profit_limit_price')
        order_type = kind.upper() + ('_LIMIT' if limit_price else '')
        params['stopPrice'] = price(args[kind])
    if exchange.id == 'binance' and order_type not in supported:
        raise ValueError(f'Native {order_type} is unsupported on this market; choose an explicit supported limit exit')
    if exchange.id == 'okx':
        params['tdMode'] = 'cash'
        params['tgtCcy'] = 'base_ccy'
    if limit_price:
        params['timeInForce'] = 'GTC'
    return amount, 'order', {'type': order_type.lower() if kind in {'market', 'limit'} else order_type,
                              'price': float(price(limit_price)) if limit_price else None, 'params': params}


def _create(mt, market, args, operation_id):
    exchange, config_id, symbol = mt.exchange, mt.config_id, market['symbol']
    request_hash = hashlib.sha256(json.dumps({'symbol': symbol, **args}, sort_keys=True).encode()).hexdigest()
    existing = next((row for row in _rows(config_id) if row['operation_id'] == operation_id), None)
    if existing:
        if existing['request_hash'] != request_hash:
            raise ValueError('Operation identity already belongs to a different spot exit')
        return _receipt(existing)
    client_id = 'sx' + hashlib.sha256(f'{config_id}:{operation_id}'.encode()).hexdigest()[:26]
    amount, adapter, request = _submission(exchange, market, args, client_id)
    stock = _inventory(mt, symbol)
    if amount > stock['available_quantity'] + 1e-10:
        raise ValueError('Sell amount exceeds verified unreserved MCP-owned inventory')
    row = {'exit_id': client_id, 'config_id': config_id, 'symbol': symbol, 'account_scope': stock['account_scope'],
           'operation_id': operation_id, 'request_hash': request_hash, 'kind': args['exit_type'],
           'amount': amount, 'filled': 0, 'state': 'submitting',
           'payload': {'adapter': adapter, 'client_id': client_id, 'request': args, 'order_ids': []}}
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT COALESCE(SUM(amount-filled),0) FROM mcp_spot_exits WHERE account_scope=? AND symbol LIKE ? AND state NOT IN (\'filled\',\'cancelled\',\'failed\')',
                               (stock['account_scope'], symbol.split('/')[0] + '/%')).fetchone()[0]
        current_filled = conn.execute('SELECT COALESCE(SUM(filled),0) FROM mcp_spot_exits WHERE config_id=? AND symbol LIKE ?',
                                      (config_id, symbol.split('/')[0] + '/%')).fetchone()[0]
        # Another worker may have reconciled a fill after the inventory read.
        # A completed exit must reduce owned quantity, not create fresh capacity.
        new_fills = max(0, float(current_filled) - stock['_exit_filled_quantity'])
        if float(current) + amount > stock['owned_quantity'] - new_fills + 1e-10:
            raise ValueError('Spot inventory was reserved by another operation')
        conn.execute('INSERT INTO mcp_spot_exits VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (client_id, config_id, symbol, row['account_scope'], operation_id, request_hash,
                      row['kind'], amount, 0, row['state'], json.dumps(row['payload']), time.time(), time.time()))
        conn.commit()
    try:
        if adapter == 'binance_oco':
            result = exchange.privatePostOrderListOco(request)
            if result.get('orderListId') is None:
                raise RuntimeError('OCO acknowledgement has no order list identity')
            row['payload']['exchange_id'] = str(result['orderListId'])
        elif adapter == 'okx_algo':
            result = _okx_row(exchange.privatePostTradeOrderAlgo(request))
            if not result.get('algoId'):
                raise RuntimeError('Algo acknowledgement has no identity')
            row['payload']['exchange_id'] = str(result['algoId'])
        else:
            result = exchange.create_order(symbol, request['type'], 'sell', amount, request['price'], request['params'])
            if not result.get('id'):
                raise RuntimeError('Sell acknowledgement has no order identity')
            row['payload']['exchange_id'] = str(result['id'])
        row['state'] = 'open'
    except (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.NotSupported) as exc:
        row['state'] = 'failed'
        row['payload']['error'] = type(exc).__name__
    except Exception as exc:
        row['state'] = 'unknown'
        row['payload']['error'] = type(exc).__name__
    _save(row)
    return _receipt(row)


def _cancel(mt, market, args):
    row = next((row for row in _rows(mt.config_id, market['symbol']) if row['exit_id'] == args['exit_id']), None)
    if not row or row['account_scope'] != account_scope(mt.exchange, mt.config_id):
        raise ValueError('Spot exit does not belong to this MCP profile and account')
    _refresh(mt.exchange, row)
    if row['state'] in _EXIT_TERMINAL:
        return _receipt(row)
    p = row['payload']
    row['state'] = 'cancelling'
    _save(row)
    try:
        if p['adapter'] == 'binance_oco':
            mt.exchange.privateDeleteOrderList({'symbol': market['id'], 'orderListId': p['exchange_id']})
        elif p['adapter'] == 'okx_algo':
            _okx_row(mt.exchange.privatePostTradeCancelAlgos([{'instId': market['id'], 'algoId': p['exchange_id']}]))
        else:
            mt.exchange.cancel_order(p['exchange_id'], market['symbol'])
        _refresh(mt.exchange, row)
    except Exception as exc:
        row['state'] = 'unknown'
        p['error'] = type(exc).__name__
        _save(row)
    return _receipt(row)


@serialized_spot_execution
def execute(tool_name, arguments, config_id, symbol, operation_id):
    args = validate_arguments(tool_name, arguments)
    mt, market = _tool(config_id, symbol)
    return _create(mt, market, args, operation_id) if tool_name == 'create_spot_exit' else _cancel(mt, market, args)


@serialized_spot_execution
def reconcile(config_id):
    """Read-only exchange reconciliation; never retries a financial submission."""
    for row in _rows(config_id):
        if row['state'] == 'failed':
            continue
        try:
            mt, _ = _tool(config_id, row['symbol'])
            _refresh(mt.exchange, row)
        except Exception:
            row['state'] = 'unknown'
            _save(row)
