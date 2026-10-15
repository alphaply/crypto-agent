"""Physical spot stock shared by quote pairs, separate from historical buy attribution."""
from __future__ import annotations
import json
import math
import time
from datetime import datetime

from backend import database
from backend.utils.visible_management import _active_management, link_resource, physical_account_scope


def _intent(config_id, symbol, operation_id, target, action, metadata):
    claim = _active_management.get()
    if claim:
        metadata = {**metadata,'physical_account':claim['physical_account']}
    with database.get_db_conn() as conn:
        existing = conn.execute('SELECT payload FROM spot_management_intents WHERE config_id=? AND operation_id=?',
                                (config_id, operation_id)).fetchone()
        if existing:
            raise ValueError('Original spot action is already recorded; query its identity instead of resubmitting')
        conn.execute('INSERT INTO spot_management_intents VALUES(?,?,?,?,?,?)',
                     (config_id,operation_id,symbol,target,action,json.dumps(metadata)))
        conn.commit()


def recover_spot_action(mt, config_id, symbol, operation_id, args, previous):
    """Query an exact recorded native mutation; never claim, cancel or amend."""
    from backend.mcp.spot_orders import _TERMINAL
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM spot_management_intents WHERE config_id=? AND operation_id=? AND symbol=?',
                           (config_id,operation_id,symbol)).fetchone()
    if not row:
        return None
    metadata = json.loads(row['payload'])
    if metadata.get('failed_result'):
        return {**metadata['failed_result'],'query':{'status':'completed','source':'definite_rejection','queried_at':time.time()}}
    result = dict(previous or {})
    try:
        if metadata.get('physical_account') != physical_account_scope(mt):
            raise ValueError('Original spot mutation belongs to another physical account')
        targets = metadata.get('children') or [row['target_id']]
        observations = [mt.exchange.fetch_order(str(identity),symbol) for identity in targets]
        for identity, observed in zip(targets, observations):
            if (str(observed.get('id')) != str(identity) or observed.get('symbol') != symbol
                    or observed.get('side') != metadata['side']):
                raise ValueError('Exact original spot target identity is not verified')
        filled_verified = all(o.get('filled') is not None for o in observations)
        if row['action'] == 'cancel':
            confirmed = filled_verified and all(str(o.get('status')).lower() in _TERMINAL for o in observations)
        else:
            observed = observations[0]
            confirmed = (filled_verified and observed.get('amount') is not None and observed.get('price') is not None
                         and math.isclose(float(observed['amount']), metadata['expected_amount'], rel_tol=1e-8,abs_tol=1e-10)
                         and math.isclose(float(observed['price']), metadata['expected_price'], rel_tol=1e-8,abs_tol=1e-10))
            if metadata['side'] == 'buy':
                from backend.utils.spot_execution import finish_spot_edit
                finish_spot_edit(config_id,operation_id,symbol,observed,confirmed=confirmed)
        if metadata.get('claim_id'):
            with database.get_db_conn() as conn:
                claim = conn.execute("SELECT * FROM visible_management_claims WHERE claim_id=? AND state='owned'", (metadata['claim_id'],)).fetchone()
            if claim:
                from backend.utils.visible_management import _decode
                link_resource(_decode(claim), metadata.get('resource_id', 'order:'+row['target_id']), None,
                              state='cancelled' if confirmed and row['action']=='cancel' else 'completed' if confirmed else 'pending')
        result.update(status='completed' if confirmed else 'pending', order_id=metadata['resource_id'] if metadata.get('children') else 'regular:'+row['target_id'],
                      order_status=observations[0].get('status'), filled_amount=sum(float(o['filled']) for o in observations) if filled_verified else None,
                      symbol=symbol, query={'status':'completed','source':'exchange_order','queried_at':time.time()})
        if row['action'] == 'edit':
            result.update(remaining_amount=observations[0].get('remaining'), price=observations[0].get('price'), edit_method='native')
    except Exception as exc:
        if result.get('status') not in {'completed','filled','cancelled','canceled'}:
            result['status']='unknown'
        result['query']={'status':'failed','source':'exchange_order','errors':[str(exc)],'queried_at':time.time()}
    return result


def _historical_inventory(mt, base, exits):
    """Net original Agent fills, independently verified; baseline is never an entry."""
    from backend.mcp.spot_orders import _order, _fees_and_fills
    with database.get_db_conn() as conn:
        buys = [dict(row) for row in conn.execute("SELECT order_id,symbol,MIN(timestamp) AS timestamp FROM orders "
                "WHERE config_id=? AND trade_mode='SPOT_DCA' AND UPPER(side) IN ('BUY','BUY_LIMIT') "
                "AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED' GROUP BY symbol,order_id", (mt.config_id,))
                if row['symbol'].split('/')[0] == base]
    if not buys:
        return {'verified_owned': 0.0, 'fee_adjustment': 0.0, 'history_sync_status': 'verified'}
    try:
        by_symbol, starts = {}, []
        for buy in buys:
            by_symbol.setdefault(buy['symbol'], []).append(_order(mt.exchange, buy['order_id'], buy['symbol'], 'buy'))
            stamp = datetime.fromisoformat(buy['timestamp'])
            if stamp.tzinfo is None:
                stamp = database.TZ_CN.localize(stamp)
            starts.append(int(stamp.timestamp() * 1000) - 60000)
        for row in exits:
            if row['config_id'] != mt.config_id:
                continue
            for order_id in row['payload'].get('order_ids') or []:
                by_symbol.setdefault(row['symbol'], []).append(_order(mt.exchange, order_id, row['symbol'], 'sell'))
        quantity = sum(_fees_and_fills(mt.exchange, symbol, orders, min(starts), strict=False)
                       for symbol, orders in by_symbol.items())
        gross = sum((1 if o['side'] == 'buy' else -1) * float(o['filled']) for orders in by_symbol.values() for o in orders)
        return {'verified_owned': max(0.0, quantity), 'fee_adjustment': gross - quantity, 'history_sync_status': 'verified'}
    except Exception:
        return {'verified_owned': None, 'fee_adjustment': None, 'history_sync_status': 'needs_refresh'}


def visible_inventory(mt, symbol, *, claim=None):
    from backend.mcp.spot_orders import _number, _managed_rows, _refresh, _receipt, _EXIT_TERMINAL
    from backend.utils.position_management import management_status
    from backend.utils.execution_ledger import account_scope
    market = mt.exchange.market(symbol)
    base = market.get('base') or symbol.split('/')[0]
    balance = mt.exchange.fetch_balance()
    asset = balance.get(base) or {}
    total = _number(asset.get('total', (balance.get('total') or {}).get(base, 0)), 'spot total balance')
    free = _number(asset.get('free', (balance.get('free') or {}).get(base, 0)), 'spot free balance')
    used = _number(asset.get('used', (balance.get('used') or {}).get(base, 0)), 'spot locked balance')
    if free > total + 1e-10 or not math.isclose(free + used, total, rel_tol=1e-7, abs_tol=1e-10):
        raise ValueError('Spot free/used balances do not form a verified total')
    projection = management_status(mt.config_id, symbol)
    scopes = projection['scopes']
    current = claim or next((c for c in scopes if c['market_type'] == 'spot' and c['resource'] == base), None)
    historical = 0.0
    with database.get_db_conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'spot_order_fills' in tables:
            historical = float(conn.execute("SELECT COALESCE(SUM(filled_qty),0) FROM spot_order_fills WHERE config_id=? AND symbol LIKE ?",
                                            (mt.config_id, base + '/%')).fetchone()[0])
        existing = conn.execute('SELECT payload FROM management_resource_links WHERE claim_id=? AND identity=?',
                                (current['claim_id'], 'inventory:' + base)).fetchone() if current else None
    baseline = json.loads(existing['payload']) if existing else {}
    exits = [r for r in _managed_rows(mt) if r['symbol'].split('/')[0] == base]
    reservations, matched_ids, pending = {}, set(), []
    for record in projection['resources']:
        if (record.get('state') in {'pending', 'unknown'} and record.get('kind') == 'external_spot_cancel'
                and record.get('side', 'sell') == 'sell'):
            pending.append(record['identity'])
            reservations[record['identity']] = float(record.get('reserved_quantity') or 0)
    for row in exits:
        try:
            observed = _refresh(mt.exchange, row)
            matched_ids.update(str(o['id']) for o in observed)
        except Exception:
            pending.append(row['exit_id'])
        if row['state'] not in _EXIT_TERMINAL:
            reservations['exit:' + row['exit_id']] = max(0, row['amount'] - row['filled'])
    symbols = [s for s, m in getattr(mt.exchange, 'markets', {}).items() if m.get('spot') and (m.get('base') or s.split('/')[0]) == base] or [symbol]
    for target in symbols:
        for order in mt.exchange.fetch_open_orders(target):
            if order.get('side') != 'sell' or str(order.get('id')) in matched_ids:
                continue
            if not order.get('id') or order.get('filled') is None or order.get('amount') is None:
                raise ValueError('A current spot sell reservation cannot be verified')
            info = order.get('info') or {}
            group = info.get('orderListId')
            identity = 'oco:' + str(group) if group is not None and str(group) != '-1' else 'order:' + str(order['id'])
            remainder = _number(order.get('remaining', float(order['amount']) - float(order['filled'])), 'spot sell remainder')
            reservations[identity] = max(reservations.get(identity, 0), remainder)
            if claim:
                link_resource(claim, identity, None, kind='external_spot_order', symbol=target,
                              source='external', order_id=str(order['id']), observed_at=time.time())
    reserved = sum(reservations.values())
    available = max(0, min(free, total - reserved)) if current and not pending else 0.0
    attributed = _historical_inventory(mt, base, exits)
    verified_owned = attributed['verified_owned']
    if claim:
        link_resource(claim, 'inventory:' + base, mt.config_id, kind='spot_inventory', symbol=symbol,
                      baseline_quantity=baseline.get('baseline_quantity', total), managed_quantity=total,
                      historical_verified_owned=verified_owned, source='baseline', observed_at=time.time())
    return {'symbol': symbol, 'base_asset': base, 'total_balance': total, 'free_balance': free,
            'owned_quantity': verified_owned, **attributed, 'historical_buy_fill_quantity': historical,
            'managed_quantity': total if current else 0,
            'managed_inventory': total if current else 0, 'baseline_quantity': baseline.get('baseline_quantity', total if current else None),
            'external_holdings': max(0, total - verified_owned) if verified_owned is not None else None,
            'reserved_quantity': reserved, 'reservations': reservations, 'available_quantity': available,
            'exchange_free': free, 'exchange_locked': used, 'untracked_holdings': max(0, total - historical),
            'account_scope': account_scope(mt.exchange, mt.config_id), 'physical_account': physical_account_scope(mt),
            'claim_owner': current['owner'] if current else None, 'claim_id': current['claim_id'] if current else None,
            'claim_status': 'owned' if current else 'eligible_unclaimed', 'observed_at': time.time(),
            'pending_orders': pending, '_exit_filled_quantity': sum(r['filled'] for r in exits),
            'exits': [_receipt(r) for r in exits]}


def execute_spot_action(config_id, symbol, action, operation_id):
    from backend.mcp.spot_orders import _tool, _create, _cancel, _managed_rows, _TERMINAL, _number, SpotExitRequest
    from backend.utils.position_management import management_action
    from backend.utils.trade_operations import financial_ack_only
    mt, market = _tool(config_id, symbol)
    with management_action(mt, symbol, 'BASE', operation_id, order_id=action.get('order_id')):
        active = _active_management.get()
        if active:
            active['outcome_status'] = 'failed'
        name = action['action']
        if name == 'open':
            from backend.agent.agent_models import OpenOrderSpotDCA
            from backend.agent.agent_tools import open_position_spot_dca
            result = open_position_spot_dca.func([OpenOrderSpotDCA(action='BUY_LIMIT', amount=action['amount'], entry_price=action['price'],
                reason=action['reason'], symbol=symbol)], config_id, symbol)
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    from backend.utils.trade_operations import tool_result_status
                    result = {'status': tool_result_status(result), 'message': result}
            if isinstance(result, dict) and isinstance(result.get('results'), list) and len(result['results']) == 1:
                result = dict(result['results'][0])
            from backend.utils.spot_execution import spot_client_order_id
            result['execution_operation_id'] = operation_id + ':0'
            result['client_id'] = spot_client_order_id(config_id, operation_id + ':0')
            result.setdefault('operation_id', operation_id)
        elif name in {'close', 'protect'}:
            stock = visible_inventory(mt, symbol)
            amount = action.get('amount') if not action.get('close_all') else stock['available_quantity']
            protection_kind = ('oco' if action.get('stop_loss') is not None and action.get('take_profit') is not None else
                               'stop_loss' if action.get('stop_loss') is not None else 'take_profit')
            kind = action.get('order_type', protection_kind if name == 'protect' else 'market')
            types = {'stop_market': 'stop_loss', 'stop_limit': 'stop_loss', 'take_profit_market': 'take_profit', 'take_profit_limit': 'take_profit'}
            args = {'amount': amount, 'exit_type': types.get(kind, kind), 'reason': action['reason']}
            if kind == 'limit':
                args['price'] = action.get('price')
            elif kind.startswith('stop'):
                args['stop_loss'] = action.get('stop_loss', action.get('trigger_price'))
                if kind == 'stop_limit' or action.get('stop_limit_price') is not None:
                    args['stop_limit_price'] = action.get('stop_limit_price', action.get('price'))
            elif kind.startswith('take_profit'):
                args['take_profit'] = action.get('take_profit', action.get('trigger_price'))
                if kind == 'take_profit_limit' or action.get('take_profit_limit_price') is not None:
                    args['take_profit_limit_price'] = action.get('take_profit_limit_price', action.get('price'))
            elif kind == 'oco' or name == 'protect':
                args.update({k: action[k] for k in ('stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price') if action.get(k) is not None})
            result = _create(mt, market, SpotExitRequest.model_validate(args).model_dump(exclude_none=True), operation_id)
        elif name == 'cancel':
            target = str(action['order_id'])
            target = target.removeprefix('spot_exit:')
            row = next((r for r in _managed_rows(mt) if target in {r['exit_id'], r['payload'].get('exchange_id')}), None)
            if row:
                result = _cancel(mt, market, {'exit_id': row['exit_id'], 'reason': action['reason']})
            elif target.startswith('oco:'):
                if getattr(mt.exchange, 'id', '') not in {'binance', 'binanceusdm'}:
                    raise ValueError('unsupported_combination: native OCO group cancellation is unavailable')
                native_id = target.split(':', 1)[1]
                native = mt.exchange.privateGetOrderList({'orderListId': native_id})
                if str(native.get('orderListId')) != native_id or native.get('symbol') != market['id']:
                    raise ValueError('Native OCO group identity is outside the managed market')
                children = [str(o['orderId']) for o in native.get('orders') or []]
                if len(children) != 2:
                    raise ValueError('Both OCO children must be verified before cancellation')
                before = [mt.exchange.fetch_order(child, symbol) for child in children]
                claim = _active_management.get()
                if any(str(o.get('id'))!=child or o.get('symbol')!=symbol or o.get('side')!='sell' for child,o in zip(children,before)):
                    raise ValueError('Native OCO children do not match the requested managed group')
                reserve = max(_number(o.get('remaining', float(o['amount'])-_number(o.get('filled'),'OCO fill')), 'OCO remainder') for o in before)
                _intent(config_id,symbol,operation_id,native_id,'cancel', {'children':children,'side':'sell','resource_id':target,'claim_id':claim['claim_id']})
                link_resource(claim, target, None, kind='external_spot_cancel', source='external',
                              symbol=symbol, side='sell',reserved_quantity=reserve, state='pending', operation_id=operation_id)
                claim['outcome_status']='unknown'
                acknowledgement = None
                try:
                    acknowledgement = mt.exchange.privateDeleteOrderList({'symbol': market['id'], 'orderListId': native_id})
                except Exception:
                    pass  # Query the original group; a lost acknowledgement is never resubmitted.
                result = ({'status':'pending' if acknowledgement is not None else 'unknown','action':'cancel',
                           'operation_id':operation_id,'order_id':target,'symbol':symbol,'filled_amount':None,
                           'submission_state':'submitted' if acknowledgement is not None else 'unknown',
                           'acknowledgement':acknowledgement} if financial_ack_only.get() else
                          recover_spot_action(mt,config_id,symbol,operation_id,action,{'status':'unknown'}))
            else:
                target = target.removeprefix('regular:')
                old = mt.exchange.fetch_order(target, symbol)
                if old.get('symbol') != symbol or str(old.get('id')) != target or old.get('side') not in {'buy','sell'}:
                    raise ValueError('Spot target identity is outside the management scope')
                claim = _active_management.get()
                reserve = _number(old.get('remaining', float(old['amount'])-_number(old.get('filled'),'spot fill')), 'spot remainder') if old['side']=='sell' else 0
                _intent(config_id,symbol,operation_id,target,'cancel',{'side':old['side'],'claim_id':claim['claim_id'],'resource_id':'order:'+target})
                link_resource(claim,'order:'+target,None,kind='external_spot_cancel',source='external',symbol=symbol,
                              side=old['side'],reserved_quantity=reserve,state='pending',operation_id=operation_id)
                claim['outcome_status']='unknown'
                acknowledgement = None
                try:
                    acknowledgement = mt.exchange.cancel_order(target, symbol)
                except Exception:
                    pass
                if financial_ack_only.get():
                    same = isinstance(acknowledgement,dict) and str(acknowledgement.get('id')) == target
                    ended = same and acknowledgement.get('status') in {'canceled','cancelled','closed','filled','expired'}
                    known_fill = ended and acknowledgement.get('filled') is not None
                    result={'status':'completed' if known_fill else 'pending' if acknowledgement is not None else 'unknown',
                            'action':'cancel','operation_id':operation_id,'order_id':'regular:'+target,'symbol':symbol,
                            'order_status':acknowledgement.get('status') if same else None,
                            'filled_amount':acknowledgement.get('filled') if same else None,'acknowledgement':acknowledgement}
                else:
                    result = recover_spot_action(mt,config_id,symbol,operation_id,action,{'status':'unknown'})
        elif name == 'edit':
            target = str(action['order_id']).removeprefix('regular:')
            order = mt.exchange.fetch_order(target, symbol)
            if order.get('symbol') != symbol or str(order.get('id')) != target or order.get('side') not in {'buy','sell'}:
                raise ValueError('Spot edit target is outside the management scope')
            if str(order.get('type')).lower() != 'limit' or not getattr(mt.exchange, 'has', {}).get('editOrder'):
                raise ValueError('unsupported_combination: native spot edit is unavailable for this target')
            remaining = action.get('remaining_amount', order.get('remaining'))
            amount = float(mt.exchange.amount_to_precision(symbol, _number(order.get('filled'), 'spot edited fills') + _number(remaining, 'spot target remainder')))
            price = float(mt.exchange.price_to_precision(symbol, action.get('price', order['price'])))
            if amount <= float(order['filled']) or price <= 0:
                raise ValueError('Spot amended quantity or price is below native precision')
            if remaining <= 0 or str(order.get('status')).lower() in _TERMINAL:
                raise ValueError('Spot edit target is no longer live')
            if order.get('side') == 'sell':
                stock = visible_inventory(mt, symbol)
                if remaining > stock['available_quantity'] + float(order.get('remaining') or 0) + 1e-10:
                    raise ValueError('Spot edit exceeds managed free inventory')
            else:
                from backend.utils.spot_execution import reserve_spot_edit
                reserve_spot_edit(config_id,mt.runtime_config,operation_id,symbol,order,amount,price,mt.exchange.fetch_balance())
            _intent(config_id,symbol,operation_id,target,'edit',{'side':order['side'],'expected_amount':amount,'expected_price':price})
            active['outcome_status']='unknown'
            acknowledgement = None
            try:
                acknowledgement = mt.exchange.edit_order(target, symbol, 'limit', order['side'], amount, price)
            except Exception as exc:
                import ccxt
                if isinstance(exc,(ccxt.InvalidOrder,ccxt.InsufficientFunds,ccxt.AuthenticationError,ccxt.PermissionDenied,ccxt.NotSupported)):
                    result={'status':'failed','error_code':type(exc).__name__,'order_id':'regular:'+target,
                            'symbol':symbol,'message':'Native amend was rejected; the original order commitment is retained.'}
                    with database.get_db_conn() as conn:
                        saved=conn.execute('SELECT payload FROM spot_management_intents WHERE config_id=? AND operation_id=?',(config_id,operation_id)).fetchone()
                        metadata=json.loads(saved[0]);metadata['failed_result']=result
                        conn.execute('UPDATE spot_management_intents SET payload=? WHERE config_id=? AND operation_id=?',(json.dumps(metadata),config_id,operation_id))
                        conn.commit()
                    if order['side']=='buy':
                        from backend.utils.spot_execution import finish_spot_reservation
                        finish_spot_reservation(config_id,operation_id,'released')
                    active['outcome_status']='failed'
                    return result
                # An uncertain amend is queried using the recorded expectation.
            if financial_ack_only.get():
                matches = (isinstance(acknowledgement,dict) and str(acknowledgement.get('id'))==target
                           and acknowledgement.get('amount') is not None and acknowledgement.get('price') is not None
                           and float(acknowledgement['amount'])==amount and float(acknowledgement['price'])==price)
                result={'status':'completed' if matches else 'pending' if acknowledgement is not None else 'unknown',
                        'action':'edit','operation_id':operation_id,'order_id':'regular:'+target,'symbol':symbol,
                        'order_status':acknowledgement.get('status') if matches else None,
                        'filled_amount':acknowledgement.get('filled') if matches else None,
                        'remaining_amount':acknowledgement.get('remaining') if matches else None,
                        'price':price,'edit_method':'native','acknowledgement':acknowledgement}
            else:
                result = recover_spot_action(mt,config_id,symbol,operation_id,action,{'status':'unknown'})
        else:
            raise ValueError('Unsupported spot action')
        from backend.utils.trade_operations import tool_result_status
        active = _active_management.get()
        if active:
            active['outcome_status'] = tool_result_status(result)
        return result
