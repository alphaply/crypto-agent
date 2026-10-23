"""Atomic simulated market/limit/conditional execution, reservations and OCO exits."""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN

from . import paper, store
from .paper_market import canonical


class PaperQuoteExpired(ValueError):
    """A temporary quote failure; existing orders retain their execution intent."""


def exposure(account, kind, symbol, side=None, exchange_name=None, *, include_excluded=False):
    from .paper_policy import visible
    collection = account['spot' if kind == 'spot' else 'positions']
    matches = [(key, p) for key, p in collection.items()
               if p.get('symbol', key) == symbol and (side is None or p.get('side') == side)
               and (exchange_name is None or p.get('exchange') == exchange_name)
               and (include_excluded or visible(p, account=account, kind='spot' if kind == 'spot' else 'positions', key=key))]
    if len(matches) > 1:
        raise ValueError('Multiple exchanges hold this instrument; specify exchange')
    return matches[0] if matches else (None, None)


def rows(conn, namespace, user_id=None, state='open'):
    from .paper_storage import decode, history_limit, max_payload, PaperPayloadLimit, blob_size, read_blob
    query, params = 'SELECT storage_id AS rowid FROM paper_orders WHERE namespace=%s', [namespace]
    if user_id is not None:
        query += ' AND user_id=%s'
        params.append(user_id)
    if state is not None:
        query += ' AND state=%s'
        params.append(state)
    query += ' ORDER BY created_at,order_id'
    metadata = conn.execute(query, params).fetchall()
    return [decode(read_blob(conn, 'paper_orders', 'payload', row['rowid']), identity='paper order') for row in metadata]


def save(conn, order):
    from .paper_storage import max_payload, PaperPayloadLimit
    order['updated_at'] = time.time()
    encoded = json.dumps(order, allow_nan=False)
    conn.execute('INSERT INTO paper_orders(namespace,user_id,order_id,state,payload,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,user_id,order_id) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at',
                 (order['namespace'], order['user_id'], order['order_id'], order['state'], encoded, order['created_at'], order['updated_at']))


def refresh_reservations(conn, account):
    cash, spot = paper.ZERO, {}
    for order in rows(conn, account['namespace'], account['user_id']):
        cash += paper.number(order.get('reserved_cash', 0))
        if order.get('reserved_spot'):
            matches = [(key, item) for key, item in account['spot'].items()
                       if item.get('symbol', key) == order['symbol']
                       and item.get('exchange') == order.get('exchange')
                       and (not order.get('cycle_id') or item.get('cycle_id') == order['cycle_id'])]
            if len(matches) == 1:
                key, _ = matches[0]
                spot[key] = float(paper.number(spot.get(key, 0)) + paper.number(order['reserved_spot']))
            else:
                # Unknown historical attribution cannot free an earlier lock or
                # attach that lock to a newly created same-symbol BN resource.
                for key, amount in account.get('reserved_spot', {}).items():
                    spot[key] = max(spot.get(key, 0), amount)
    account.update(reserved_cash=paper.money(cash), reserved_spot=spot)


def finish(conn, account, order, state, reason):
    order.update(state=state, reason=reason, reserved_cash=0, reserved_spot=0)
    save(conn, order)
    paper._event(conn, account, 'order_' + state, {'order_id': order['order_id'], 'symbol': order['symbol'],
                                                'exchange': order['exchange'], 'reason': reason, 'automatic': True})


def cleanup_orders(conn, account):
    from .paper_policy import visible
    for order in rows(conn, account['namespace'], account['user_id']):
        if not visible(order, account=account, conn=conn):
            continue
        if order['action'] in {'buy', 'open_long', 'open_short'}:
            continue
        side = 'LONG' if order['action'] == 'close_long' else 'SHORT' if order['action'] == 'close_short' else None
        _, p = exposure(account, order['market_type'], order['symbol'], side, order['exchange'])
        if not p or (order.get('cycle_id') and p.get('cycle_id') != order['cycle_id']):
            finish(conn, account, order, 'cancelled', 'Position cycle ended or liquidated')
        elif not order.get('close_all') and paper.number(order['amount']) > paper.number(p['amount']):
            finish(conn, account, order, 'cancelled', 'Remaining exposure is smaller than the exit quantity')
    refresh_reservations(conn, account)


def validate(kind, action, amount=None, quote_amount=None, margin_amount=None, close_all=False, leverage=None):
    if kind not in {'spot', 'swap'} or action not in ({'buy', 'sell'} if kind == 'spot' else {'open_long', 'open_short', 'close_long', 'close_short'}):
        raise ValueError('Action does not match spot/perpetual market')
    if type(close_all) is not bool:
        raise ValueError('close_all must be a boolean')
    exit_action = action in {'sell', 'close_long', 'close_short'}
    provided = sum(v is not None for v in (amount, quote_amount, margin_amount)) + bool(close_all)
    if provided != 1:
        raise ValueError('Provide exactly one of amount, quote_amount, margin_amount or close_all')
    if close_all and not exit_action or quote_amount is not None and exit_action or margin_amount is not None and (kind != 'swap' or exit_action):
        raise ValueError('Budget sizing is for entries; close_all is for exits')
    if leverage is not None and (kind != 'swap' or exit_action):
        raise ValueError('Leverage applies only to new perpetual entries')
    if leverage is not None and (type(leverage) is not int or not 1 <= leverage <= paper.settings()['max_leverage']):
        raise ValueError('Leverage exceeds the configured paper maximum')
    for value in (amount, quote_amount, margin_amount):
        if value is not None:
            paper.number(value, positive=True)
    if amount is not None and paper.number(amount) != paper.number(amount).quantize(Decimal('.000000000001')):
        raise ValueError('Base quantity supports up to 12 decimal places')


def quantity(account, kind, symbol, action, reference, exchange_name, *, amount=None, quote_amount=None, margin_amount=None, close_all=False, leverage=None):
    if close_all:
        side = 'LONG' if action == 'close_long' else 'SHORT' if action == 'close_short' else None
        _, p = exposure(account, kind, symbol, side, exchange_name)
        if not p:
            raise ValueError('No matching position or spot holding to close')
        return paper.number(p['amount'], positive=True)
    if amount is not None:
        return paper.number(amount, positive=True)
    price = paper.number(reference, positive=True)
    if margin_amount is not None:
        lev = paper.number(leverage or account['leverage'])
        if kind == 'swap':
            fee_rate = paper.number(paper.settings()['fee_rate'])
            effective_margin = paper.number(margin_amount, positive=True) / (paper.number(1) + lev * fee_rate)
            qty = effective_margin * lev / price
        else:
            qty = paper.number(margin_amount, positive=True) * lev / price
    elif kind == 'spot':
        qty = paper.number(quote_amount, positive=True) / (price * (1 + paper.number(paper.settings()['fee_rate'])))
    else:
        qty = paper.number(quote_amount, positive=True) / price
    qty = qty.quantize(Decimal('.000000000001'), rounding=ROUND_DOWN)
    return paper.number(qty, positive=True)


def existing_receipt(caller, user_id, operation_id, request):
    namespace = paper.require(caller, 'trade')
    paper.require(caller, 'read')
    paper.identity(user_id)
    with store.connection() as conn:
        original = paper._receipt(conn, namespace, user_id, operation_id, request)[1]
        if original is None:
            return None
        from .paper_storage import one
        revision = conn.execute('SELECT revision FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s ORDER BY revision DESC LIMIT 1',
                                (namespace, user_id, operation_id)).fetchone()
        return one(conn, 'paper_receipt_revisions', 'payload', 'namespace=%s AND user_id=%s AND operation_id=%s AND revision=%s',
                   (namespace, user_id, operation_id, revision[0]), identity='latest original paper receipt') if revision else original


def get_quote(symbol, kind, exchange_name=None):
    from .paper_policy import venue
    selected = venue(exchange_name)
    result = paper.market(symbol, kind, candles=False, exchange_name=selected, include=['ticker'])
    venue(result['exchange'])
    return result


def cancel_exits(conn, account, kind, symbol, exchange_name, side=None, *, only_protection=False):
    from .paper_policy import visible
    for order in rows(conn, account['namespace'], account['user_id']):
        if not visible(order, account=account, conn=conn):
            continue
        if (order['market_type'], order['symbol'], order['exchange']) != (kind, symbol, exchange_name):
            continue
        if order['action'] not in ({'sell'} if kind == 'spot' else {'close_' + side.lower()} if side else {'close_long', 'close_short'}):
            continue
        if only_protection and not order.get('oco_group'):
            continue
        finish(conn, account, order, 'cancelled', 'Replaced protection or explicit close-all')
    refresh_reservations(conn, account)


def add_protection(conn, account, kind, symbol, exchange_name, side, stop_loss, take_profit, amount=None, close_all=True, *, reference=None):
    from .paper_policy import stamp, visible
    _, p = exposure(account, kind, symbol, side if kind == 'swap' else None, exchange_name)
    if not p:
        raise ValueError('No matching exposure for protection')
    if amount is not None and paper.number(amount, positive=True) > paper.number(p['amount']):
        raise ValueError('Protection quantity exceeds exposure')
    old = [o for o in rows(conn, account['namespace'], account['user_id']) if visible(o, account=account, conn=conn) and o.get('oco_group') and o['symbol'] == symbol and o['exchange'] == exchange_name and o['market_type'] == kind and o.get('position_side') == side]
    stop_loss = stop_loss if stop_loss is not None else next((o['trigger_price'] for o in old if o['order_type'] == 'stop_loss'), None)
    take_profit = take_profit if take_profit is not None else next((o['trigger_price'] for o in old if o['order_type'] == 'take_profit'), None)
    if stop_loss is None and take_profit is None:
        raise ValueError('Provide stop_loss and/or take_profit')
    for order_type, price in [('stop_loss', stop_loss), ('take_profit', take_profit)]:
        if price is None:
            continue
        trigger = paper.number(price, positive=True)
        if reference is not None:
            below = order_type == 'stop_loss' if side == 'LONG' else order_type == 'take_profit'
            if (below and trigger >= paper.number(reference)) or (not below and trigger <= paper.number(reference)):
                raise ValueError('Protection trigger is on the wrong side of the current/reference price')
    cancel_exits(conn, account, kind, symbol, exchange_name, side, only_protection=True)
    group, result = uuid.uuid4().hex, []
    for order_type, trigger in [('stop_loss', stop_loss), ('take_profit', take_profit)]:
        if trigger is None:
            continue
        order = {'namespace': account['namespace'], 'user_id': account['user_id'], 'order_id': uuid.uuid4().hex,
                 'state': 'open', 'market_type': kind, 'symbol': symbol, 'exchange': exchange_name,
                 'action': 'sell' if kind == 'spot' else 'close_' + side.lower(), 'order_type': order_type,
                 'trigger_price': paper.money(paper.number(trigger, positive=True)), 'price': None,
                 'amount': float(paper.number(amount)) if amount is not None else p['amount'], 'close_all': close_all,
                 'position_side': side, 'cycle_id': p.get('cycle_id'), 'oco_group': group,
                 'reserved_cash': 0, 'reserved_spot': 0, 'created_at': time.time(), 'execution_mode': 'paper'}
        stamp(order)
        save(conn, order)
        result.append(order)
    return result


def execute_market(caller, user_id, kind, symbol, action, operation_id, *, amount=None, quote_amount=None, margin_amount=None, close_all=False, leverage=None, exchange_name=None, stop_loss=None, take_profit=None):
    wanted = canonical(symbol, kind)
    if (stop_loss is not None or take_profit is not None) and action not in {'buy', 'open_long', 'open_short'}:
        raise ValueError('Attached protection is only accepted on entries')
    request = {'action': action, 'market_type': kind, 'symbol': wanted, 'amount': str(paper.number(amount)) if amount is not None else None,
               'quote_amount': quote_amount, 'margin_amount': margin_amount, 'close_all': close_all,
               'leverage': leverage, 'exchange': exchange_name, 'stop_loss': stop_loss, 'take_profit': take_profit}
    previous = existing_receipt(caller, user_id, operation_id, request)
    if previous is not None:
        return previous
    validate(kind, action, amount, quote_amount, margin_amount, close_all, leverage)
    if action in {'sell', 'close_long', 'close_short'}:
        _require_exposure(caller, user_id, kind, wanted, 'LONG' if action == 'close_long' else 'SHORT' if action == 'close_short' else None)
    quote = get_quote(wanted, kind, exchange_name)
    cfg = paper.settings()
    def change(account, conn):
        if close_all:
            side = 'LONG' if action == 'close_long' else 'SHORT' if action == 'close_short' else None
            cancel_exits(conn, account, kind, wanted, quote['exchange'], side)
        reference = quote['ask'] if action in {'buy', 'open_long', 'close_short'} else quote['bid']
        qty = quantity(account, kind, wanted, action, reference, quote['exchange'], amount=amount, quote_amount=quote_amount,
                       margin_amount=margin_amount, close_all=close_all, leverage=leverage)
        details = paper._fill(account, conn, quote, cfg, kind, wanted, action, qty, leverage)
        if stop_loss is not None or take_profit is not None:
            side = 'SHORT' if action == 'open_short' else 'LONG'
            details['protection_orders'] = add_protection(conn, account, kind, wanted, quote['exchange'], side, stop_loss, take_profit, reference=quote['price'])
        return details
    return paper._write(caller, user_id, operation_id, request, change)


def prepare_order(conn, account, kind, symbol, action, order_type, quote, *, amount=None, quote_amount=None, margin_amount=None, close_all=False, leverage=None, price=None, trigger_price=None, stop_loss=None, take_profit=None, order_id=None):
    from .paper_policy import stamp
    validate(kind, action, amount, quote_amount, margin_amount, close_all, leverage)
    if not account['enabled'] or kind not in account['markets']:
        raise ValueError('Paper account or requested market is disabled')
    if order_type not in {'limit', 'stop_loss', 'take_profit'}:
        raise ValueError('order_type must be limit, stop_loss or take_profit')
    if order_type == 'limit':
        if price is None or trigger_price is not None:
            raise ValueError('A limit order requires price and no trigger_price')
        reference = paper.number(price, positive=True)
    else:
        if action not in {'sell', 'close_long', 'close_short'} or trigger_price is None or price is not None:
            raise ValueError('Conditional orders require an exit action and trigger_price, without price')
        reference = paper.number(quote['price'], positive=True)
        paper.number(trigger_price, positive=True)
    qty = quantity(account, kind, symbol, action, reference, quote['exchange'], amount=amount, quote_amount=quote_amount,
                   margin_amount=margin_amount, close_all=close_all, leverage=leverage)
    side = 'LONG' if action in {'open_long', 'close_long', 'buy', 'sell'} else 'SHORT'
    _, p = exposure(account, kind, symbol, side if kind == 'swap' else None, quote['exchange'])
    reserve, spot_reserve = paper.ZERO, paper.ZERO
    if action in {'buy', 'open_long', 'open_short'}:
        view = paper.snapshot(account, conn=conn)
        if view['net_equity'] is not None and view['net_equity'] <= 0:
            raise ValueError('Cannot open new positions when net equity is non-positive; reduce debt or close positions first')
        lev = leverage or account['leverage'] if kind == 'swap' else 1
        if lev > paper.settings()['max_leverage'] or kind == 'swap' and 1 / lev <= paper.settings()['maintenance_margin_rate'] + paper.settings()['fee_rate']:
            raise ValueError('Leverage leaves insufficient maintenance margin or exceeds the current cap')
        if p and kind == 'swap' and p['leverage'] != lev:
            raise ValueError('Existing isolated position has different leverage')
        reserve = reference * qty * (paper.number(paper.settings()['fee_rate']) + paper.number(1) / lev)
        if reserve > paper.signed_cash(account['cash']) - paper.number(account.get('reserved_cash', 0)):
            raise ValueError('Insufficient unreserved cash for the limit order')
        leverage = lev if kind == 'swap' else None
    else:
        if not p or qty > paper.number(p['amount']):
            raise ValueError('Exit quantity exceeds the current exposure')
        if kind == 'spot' and order_type == 'limit':
            key, _ = exposure(account, kind, symbol, exchange_name=quote['exchange'])
            if qty > paper.number(p['amount']) - paper.number(account.get('reserved_spot', {}).get(key, 0)):
                raise ValueError('Spot quantity is reserved by another order')
            spot_reserve = qty
    if stop_loss is not None or take_profit is not None:
        if action not in {'buy', 'open_long', 'open_short'}:
            raise ValueError('Attached protection is only for entries')
        for role, value in [('stop_loss', stop_loss), ('take_profit', take_profit)]:
            if value is None:
                continue
            value = paper.number(value, positive=True)
            below = role == 'stop_loss' if side == 'LONG' else role == 'take_profit'
            if below and value >= reference or not below and value <= reference:
                raise ValueError('Attached protection is on the wrong side of the limit entry price')
    return stamp({'namespace': account['namespace'], 'user_id': account['user_id'], 'order_id': order_id or uuid.uuid4().hex,
            'state': 'open', 'market_type': kind, 'symbol': symbol, 'exchange': quote['exchange'], 'action': action,
            'order_type': order_type, 'price': paper.money(reference) if order_type == 'limit' else None,
            'trigger_price': paper.money(paper.number(trigger_price)) if trigger_price is not None else None,
            'amount': float(qty), 'close_all': close_all, 'leverage': leverage, 'position_side': side,
            'cycle_id': p.get('cycle_id') if p and action in {'sell', 'close_long', 'close_short'} else None,
            'stop_loss': stop_loss, 'take_profit': take_profit, 'reserved_cash': paper.money(reserve),
            'reserved_spot': float(spot_reserve), 'created_at': time.time(), 'execution_mode': 'paper'})


def place(caller, user_id, kind, symbol, action, order_type, operation_id, **kwargs):
    exchange_name = kwargs.pop('exchange_name', None)
    wanted = canonical(symbol, kind)
    request = {'action': 'place_order', 'market_type': kind, 'symbol': wanted, 'trade_action': action,
               'order_type': order_type, 'exchange': exchange_name, **kwargs}
    previous = existing_receipt(caller, user_id, operation_id, request)
    if previous is not None:
        return previous
    if action in {'sell', 'close_long', 'close_short'}:
        _require_exposure(caller, user_id, kind, wanted, 'LONG' if action == 'close_long' else 'SHORT' if action == 'close_short' else None)
    quote = get_quote(wanted, kind, exchange_name)
    def change(account, conn):
        order = prepare_order(conn, account, kind, wanted, action, order_type, quote, **kwargs)
        save(conn, order)
        return {'order': order, 'note': 'Accepted for simulated matching; acceptance is not a fill.'}
    return paper._write(caller, user_id, operation_id, request, change)


def get_orders(caller, user_id, state='open', limit=100):
    result = records_page(caller, user_id, state=state, limit=limit)
    return {'orders': result['items'], 'total': len(result['items']) if result['scan_complete'] else None,
            **{key: value for key, value in result.items() if key != 'items'}}


def records_page(caller, user_id, state='open', limit=100, offset=0, *, cursor=0, symbol=None, order_id=None, detail=False):
    namespace = paper.require(caller)
    paper.identity(user_id)
    if state not in {'open', 'filled', 'cancelled', 'rejected', 'all'} or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Invalid state or limit')
    from .paper_storage import one, payload_page
    from .paper_policy import visible
    with store.connection() as conn:
        account = {'namespace': namespace, 'user_id': user_id}
        if not conn.execute('SELECT 1 FROM paper_accounts WHERE namespace=%s AND user_id=%s', (namespace, user_id)).fetchone():
            raise ValueError('Paper account does not exist')
        if order_id is not None:
            # Exact historical identities are an authorized passive audit path.
            order = one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s',
                        (namespace, user_id, order_id), identity='paper order audit')
            if order is None:
                raise ValueError('Order not found in this account')
            return {'order': order, 'execution_mode': 'paper'}
        where, params = 'namespace=%s AND user_id=%s', [namespace, user_id]
        if state != 'all':
            where += ' AND state=%s'
            params.append(state)
        if offset:
            selected = conn.execute('SELECT storage_id AS rowid FROM paper_orders WHERE ' + where + ' ORDER BY storage_id LIMIT 1 OFFSET %s',
                                    (*params, offset - 1)).fetchone()
            cursor = selected[0] if selected else conn.execute('SELECT COALESCE(MAX(storage_id),0) FROM paper_orders').fetchone()[0]
        found, page = payload_page(conn, 'paper_orders', where, params, cursor=cursor, count=limit)
        items = [row['payload'] for row in found if visible(row['payload'], account=account, conn=conn)
                 and (not symbol or row['payload'].get('symbol') == symbol)]
        conn.commit()
    return {'items': items, **page, 'filtered_count': len(found) - len(items), 'execution_mode': 'paper'}


def cancel(caller, user_id, order_id, operation_id):
    from .paper_policy import require_target
    def change(account, conn):
        from .paper_storage import one
        found = one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s', (account['namespace'], user_id, order_id))
        if not found:
            raise ValueError('Order not found in this account')
        require_target(found, account=account, conn=conn)
        if found['state'] != 'open':
            return {'order': found, 'changed': False}
        finish(conn, account, found, 'cancelled', 'Requested by account owner')
        return {'order': found, 'changed': True}
    return paper._write(caller, user_id, operation_id, {'action': 'cancel_order', 'order_id': order_id}, change)


def edit(caller, user_id, order_id, operation_id, price=None, trigger_price=None, amount=None):
    request = {'action': 'edit_order', 'order_id': order_id, 'price': price, 'trigger_price': trigger_price, 'amount': amount}
    previous = existing_receipt(caller, user_id, operation_id, request)
    if previous is not None:
        return previous
    namespace = paper.require(caller, 'trade')
    from .paper_storage import one
    with store.connection() as conn:
        order = one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s', (namespace, user_id, order_id))
    if not order or order['state'] != 'open':
        raise ValueError('Only an open order in this account can be edited')
    from .paper_policy import require_target
    with store.connection() as conn:
        require_target(order, account=paper._get(conn, namespace, user_id), conn=conn)
    quote = get_quote(order['symbol'], order['market_type'], order['exchange'])
    def change(account, conn):
        current = one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s', (namespace, user_id, order_id))
        if not current or current['state'] != 'open' or current.get('updated_at') != order.get('updated_at'):
            raise ValueError('Order changed during edit; inspect its current state')
        current['state'] = 'editing'
        save(conn, current)
        refresh_reservations(conn, account)
        updated = prepare_order(conn, account, current['market_type'], current['symbol'], current['action'], current['order_type'], quote,
                                amount=amount if amount is not None else None if current.get('close_all') else current['amount'],
                                close_all=current.get('close_all', False) if amount is None else False,
                                leverage=current.get('leverage'), price=price if price is not None else current.get('price'),
                                trigger_price=trigger_price if trigger_price is not None else current.get('trigger_price'),
                                stop_loss=current.get('stop_loss'), take_profit=current.get('take_profit'), order_id=order_id)
        updated.update(created_at=current['created_at'], oco_group=current.get('oco_group'),
                       resource_id=current.get('resource_id', updated['resource_id']),
                       revision=current.get('revision', 0) + 1)
        save(conn, updated)
        return {'order': updated}
    return paper._write(caller, user_id, operation_id, request, change)


def protection(caller, user_id, kind, symbol, side, operation_id, stop_loss=None, take_profit=None, amount=None, close_all=True, exchange_name=None):
    if kind not in {'spot', 'swap'} or side not in {'LONG', 'SHORT'} or kind == 'spot' and side != 'LONG':
        raise ValueError('Spot protection is LONG; perpetual protection uses LONG or SHORT')
    if amount is not None:
        close_all = False
    wanted = canonical(symbol, kind)
    request = {'action': 'set_protection', 'market_type': kind, 'symbol': wanted, 'side': side, 'stop_loss': stop_loss,
               'take_profit': take_profit, 'amount': amount, 'close_all': close_all, 'exchange': exchange_name}
    previous = existing_receipt(caller, user_id, operation_id, request)
    if previous is not None:
        return previous
    _require_exposure(caller, user_id, kind, wanted, side if kind == 'swap' else None)
    quote = get_quote(wanted, kind, exchange_name)
    def change(account, conn):
        result = add_protection(conn, account, kind, wanted, quote['exchange'], side, stop_loss, take_profit, amount, close_all, reference=quote['price'])
        return {'orders': result, 'oco': True}
    return paper._write(caller, user_id, operation_id, request, change)


def edit_remaining(caller, user_id, order_id, operation_id, price=None, trigger_price=None, remaining_amount=None):
    """V3 edits never silently turn cycle-following all into fixed quantity."""
    namespace = paper.require(caller, 'trade')
    with store.connection() as conn:
        target = next((o for o in rows(conn, namespace, user_id) if o['order_id'] == order_id), None)
    if target and target.get('close_all') and remaining_amount is not None:
        raise ValueError('A close_all order cannot change remaining_amount')
    if price is None and trigger_price is None and remaining_amount is None:
        raise ValueError('Provide price, trigger_price or remaining_amount')
    return edit(caller, user_id, order_id, operation_id, price, trigger_price, remaining_amount)


def triggered(order, quote):
    if order['order_type'] == 'limit':
        buy = order['action'] in {'buy', 'open_long', 'close_short'}
        return quote['ask'] <= order['price'] if buy else quote['bid'] >= order['price']
    below = order['order_type'] == 'stop_loss' if order['position_side'] == 'LONG' else order['order_type'] == 'take_profit'
    return quote['price'] <= order['trigger_price'] if below else quote['price'] >= order['trigger_price']


def match(conn, account, kind, symbol, quote, cfg):
    if account.get('account_status') == 'bankrupt':
        return
    from .paper_policy import visible
    cleanup_orders(conn, account)
    for order in rows(conn, account['namespace'], account['user_id']):
        if not visible(order, account=account, conn=conn):
            continue
        current = conn.execute('SELECT state FROM paper_orders WHERE namespace=%s AND user_id=%s AND order_id=%s',
                               (account['namespace'], account['user_id'], order['order_id'])).fetchone()
        if not current or current[0] != 'open':
            continue
        if (order['market_type'], order['symbol'], order['exchange']) != (kind, symbol, quote['exchange']):
            continue
        if not account['enabled'] and order['action'] in {'buy', 'open_long', 'open_short'}:
            continue
        if not triggered(order, quote):
            continue
        conn.execute('SAVEPOINT paper_match')
        original = json.loads(json.dumps(account))
        try:
            order.update(state='matching', reserved_cash=0, reserved_spot=0)
            save(conn, order)
            if order.get('close_all') and order['action'] in {'sell', 'close_long', 'close_short'}:
                cancel_exits(conn, account, kind, symbol, quote['exchange'], order.get('position_side'))
            refresh_reservations(conn, account)
            qty = quantity(account, kind, symbol, order['action'], quote['price'], quote['exchange'],
                           amount=None if order.get('close_all') else order['amount'], close_all=order.get('close_all', False))
            details = paper._fill(account, conn, quote, cfg, kind, symbol, order['action'], qty, order.get('leverage'))
            if order.get('stop_loss') is not None or order.get('take_profit') is not None:
                details['protection_orders'] = add_protection(conn, account, kind, symbol, quote['exchange'], order['position_side'], order.get('stop_loss'), order.get('take_profit'))
            order.update(state='filled', fill=details['fill'], filled_at=time.time(), filled_amount=float(qty))
            save(conn, order)
            if order.get('oco_group'):
                for sibling in rows(conn, account['namespace'], account['user_id']):
                    if sibling.get('oco_group') == order['oco_group'] and sibling['order_id'] != order['order_id']:
                        finish(conn, account, sibling, 'cancelled', 'OCO sibling filled')
            paper._event(conn, account, 'order_filled', {'order_id': order['order_id'], 'order_type': order['order_type'],
                                                       'automatic': True, **details}, order.get('operation_id'))
            conn.execute('RELEASE paper_match')
        except PaperQuoteExpired:
            conn.execute('ROLLBACK TO paper_match')
            conn.execute('RELEASE paper_match')
            account.clear()
            account.update(original)
            # Nothing was submitted to an exchange. Keep the original pending
            # order and any OCO/reservations until maintenance gets a new quote.
            return
        except ValueError as exc:
            conn.execute('ROLLBACK TO paper_match')
            conn.execute('RELEASE paper_match')
            account.clear()
            account.update(original)
            finish(conn, account, order, 'rejected', str(exc))
        refresh_reservations(conn, account)


def _require_exposure(caller, user_id, kind, symbol, side):
    namespace = paper.require(caller, 'trade')
    with store.connection() as conn:
        account = paper._get(conn, namespace, user_id)
        _, target = exposure(account, kind, symbol, side)
        if target is None:
            raise ValueError('paper_origin_unverified: no eligible Binance exposure; raw commitments retained')


def admin_orders(namespace=None, user_id=None, state='open', limit=100, *, cursor=0):
    if state not in {'open', 'filled', 'cancelled', 'rejected', 'all'} or not 1 <= limit <= 200:
        raise ValueError('Invalid state or limit')
    with store.connection() as conn:
        from .paper_storage import payload_page
        from .paper_policy import visible
        clauses, params = [], []
        for column, value in [('namespace', namespace), ('user_id', user_id), ('state', state if state != 'all' else None)]:
            if value:
                clauses.append(column + '=%s')
                params.append(value)
        where = ' AND '.join(clauses) or '1=1'
        count = conn.execute('SELECT COUNT(*) FROM paper_orders WHERE ' + where, params).fetchone()[0]
        records, progress = payload_page(conn, 'paper_orders', where, params, cursor=cursor, count=limit)
        result = [row['payload'] for row in records if visible(row['payload'], conn=conn, account={'namespace': row['namespace'], 'user_id': row['user_id']})]
        conn.commit()
    return {'orders': result, 'total': count, 'filtered_count': len(records) - len(result), **progress}


from .paper_storage import bounded as _bounded
from .paper_lifecycle import capture_epoch as _capture_epoch
for _name in ('execute_market', 'place', 'cancel', 'edit', 'protection', 'edit_remaining'):
    globals()[_name] = _capture_epoch(globals()[_name])
for _name in ('existing_receipt', 'execute_market', 'place', 'cancel', 'edit', 'protection', 'edit_remaining', 'records_page', 'admin_orders'):
    globals()[_name] = _bounded(globals()[_name])
