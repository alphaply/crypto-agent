"""Adapters for v3 intent; legacy call paths and their serializers stay intact."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
import time

from backend.utils.trade_operations import _decode_receipt, tool_result_status


def _native_receipt(result, action, operation_id, symbol, *, contract_size=1):
    value = _decode_receipt(result)
    if not isinstance(value, dict):
        return {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id,
                'status': tool_result_status(value), 'message': str(value), 'symbol': symbol}
    receipt = dict(value)
    state = str(receipt.get('order_status') or receipt.get('status') or '').lower()
    if state == 'no_position':
        state = 'skipped'
    explicit = receipt.get('status')
    receipt['status'] = (explicit if explicit in {'completed', 'submitted', 'failed', 'pending', 'unknown', 'skipped', 'not_executed'} else
                         'submitted' if state in {'open', 'new', 'waiting_trigger', 'partially_filled'} else
                         tool_result_status({**receipt, 'status': state}))
    if receipt.get('id') and not receipt.get('order_id'):
        receipt['order_id'] = 'regular:' + str(receipt['id'])
    if state in {'open', 'new', 'closed', 'filled', 'canceled', 'cancelled', 'waiting_trigger', 'partially_filled'}:
        receipt.setdefault('order_status', {'closed': 'filled', 'cancelled': 'canceled'}.get(state, state))
    if 'filled' in receipt and 'filled_amount' not in receipt:
        receipt['filled_amount'] = None if receipt['filled'] is None else float(receipt['filled']) * contract_size
    if 'remaining' in receipt and receipt['remaining'] is not None:
        receipt.setdefault('remaining_amount', float(receipt['remaining']) * contract_size)
    if 'amount' in receipt and receipt.get('amount') is not None and contract_size != 1:
        receipt['amount'] = float(receipt['amount']) * contract_size
    receipt.update(contract_version=3, receipt_version=1, operation_id=operation_id, action=action.action, symbol=symbol)
    if hasattr(action, 'side'):
        if str(receipt.get('side', '')).lower() in {'buy', 'sell'}:
            receipt['exchange_side'] = receipt['side']
        receipt['side'] = action.side
    if hasattr(action, 'close_all'):
        receipt.setdefault('quantity_mode', 'all' if action.close_all else 'fixed')
    return receipt


def _independent(action, config_id, symbol, cfg, operation_id, mt):
    from backend.agent.agent_models import OpenOrderStrategy
    from backend.database_independent import MockIndependentTrading
    from backend.utils.independent_exits import IndependentExits
    from backend.utils.trade_receipts import TradeAttempt, current_attempt, project_receipt
    real = cfg.get('mode', 'STRATEGY').upper() == 'REAL'
    service = IndependentExits(mt) if real else MockIndependentTrading(config_id, symbol)
    args = action.model_dump(exclude_none=True, exclude={'action', 'symbol', 'market_type', 'exchange', 'depends_on'})
    if action.action == 'open':
        args = {'op': OpenOrderStrategy(action='BUY_LIMIT' if action.side == 'LONG' else 'SELL_LIMIT',
                                       amount=action.amount, entry_price=action.price, reason=action.reason,
                                       valid_duration_hours=action.valid_duration_hours or 24), 'contract_version': 2}
    elif action.action == 'close':
        args['pos_side'] = args.pop('side')
        args['exit_type'] = args.pop('order_type')
        args['contract_version'] = 2
        args.setdefault('amount', None)
    if action.action == 'cancel':
        args.pop('reason')
    if not real and action.action in {'close', 'edit'}:
        args['current_price'] = float(mt.exchange.fetch_ticker(symbol)['last'])
    args['operation_id'] = operation_id
    attempt = TradeAttempt(operation_id, action.action)
    attempt.arguments = action.model_dump(exclude_none=True)
    token = current_attempt.set(attempt)
    try:
        method = getattr(service, action.action)
        from backend.utils.position_protection import ConcurrentProtectionUpdate
        for refresh in range(2):
            try:
                result = method(symbol, **args) if real else method(**args)
                break
            except ConcurrentProtectionUpdate:
                if refresh or attempt.phase != 'preflight' or attempt.submission_state != 'not_submitted':
                    raise
                attempt.plan, attempt.record, attempt.admission = None, None, None
        if not real:
            # Simulation methods already own the atomic transaction and result.
            state = result.get('order_status') or result.get('status')
            result['status'] = ('completed' if action.action in {'edit', 'cancel'} or state in {'filled', 'closed', 'canceled', 'cancelled'} else
                                'submitted' if state in {'open', 'waiting_trigger', 'partially_filled'} else tool_result_status(result))
        return _native_receipt(result, action, operation_id, symbol)
    except Exception as exc:
        result = project_receipt(attempt.plan or getattr(exc, 'plan', None), attempt.record,
                                 operation_id=operation_id, action=action.action, phase=attempt.phase,
                                 submission_state=attempt.submission_state, error=str(exc))
        if hasattr(exc, 'code'):
            result.update(error_code=exc.code, details=getattr(exc, 'details', {}))
        return result
    finally:
        current_attempt.reset(token)


def _raw_order_id(identity):
    channel, separator, value = str(identity).partition(':')
    if separator and channel in {'regular', 'algo'}:
        return value
    return str(identity)


def _native_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS unified_native_intents (
        config_id TEXT NOT NULL,operation_id TEXT NOT NULL,symbol TEXT NOT NULL,
        account_scope TEXT NOT NULL,client_id TEXT NOT NULL,request_json TEXT NOT NULL,
        result TEXT,PRIMARY KEY(config_id,operation_id))''')


def _attached_close(action, config_id, symbol, operation_id, mt, service):
    from backend import database
    from backend.utils.independent_exits import validate_exit
    market = service._market(symbol)
    positions = mt.exchange.fetch_positions([symbol])
    position = next((pos for pos in positions if str(pos.get('side')).upper() == action.side and float(pos.get('contracts') or 0) > 0), None)
    if not position:
        return {'status': 'skipped', 'position_amount': 0}
    size = float(market.get('contractSize') or 1)
    available = float(position['contracts']) * size
    amount = available if action.close_all else action.amount
    if amount > available + 1e-10:
        raise ValueError('Fixed close quantity exceeds the current position; use close_all for an explicit full close')
    current_price = float(mt.exchange.fetch_ticker(symbol)['last'])
    kind = 'take_profit_limit' if action.order_type == 'limit' else action.order_type
    validate_exit(kind, action.side, current_price, action.price, action.trigger_price)
    quantity = float(mt.exchange.amount_to_precision(symbol, amount / size))
    if quantity <= 0:
        raise ValueError('Close quantity is below the market precision')
    client_id = 'v3c' + hashlib.sha256(f'{config_id}\0{operation_id}'.encode()).hexdigest()[:28]
    hedged = bool(position.get('hedged', True))
    is_okx = mt.exchange.id == 'okx'
    params = {'hedged': hedged, 'reduceOnly': True} if is_okx else {'positionSide': action.side if hedged else 'BOTH'}
    if not hedged:
        params['reduceOnly'] = True
    params['clientOrderId'] = client_id
    native_kind, price = ('limit', float(mt.exchange.price_to_precision(symbol, action.price))) if action.order_type == 'limit' else ('market', None)
    if action.order_type == 'stop_market':
        params['stopLossPrice' if is_okx else 'stopPrice'] = float(mt.exchange.price_to_precision(symbol, action.trigger_price))
        native_kind = 'market' if is_okx else 'STOP_MARKET'
    intent = {'action': action.model_dump(exclude_none=True), 'native_type': native_kind,
              'amount': quantity, 'price': price, 'params': params, 'contract_size': size}
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _native_table(conn)
        conn.execute('INSERT INTO unified_native_intents VALUES(?,?,?,?,?,?,NULL)',
                     (config_id, operation_id, symbol, service.account_scope, client_id, json.dumps(intent, sort_keys=True)))
        conn.commit()
    try:
        result = mt.exchange.create_order(symbol, native_kind, 'sell' if action.side == 'LONG' else 'buy', quantity, price, params)
    except Exception as exc:
        import ccxt
        rejected = isinstance(exc, (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.NotSupported))
        result = {'status': 'failed' if rejected else 'unknown', 'error': str(exc), 'error_code': type(exc).__name__,
                  'client_id': client_id, 'phase': 'submit', 'submission_state': 'rejected' if rejected else 'unknown',
                  'recovery': {'state': 'background', 'target_operation_id': operation_id}}
    result = {**result, 'client_id': client_id}
    if str(result.get('status')).lower() in {'closed', 'filled'} and result.get('filled') is None:
        result.update(status='pending', order_status='filled', error_code='fill_unverified')
    with database.get_db_conn() as conn:
        conn.execute('UPDATE unified_native_intents SET result=? WHERE config_id=? AND operation_id=?',
                     (json.dumps(result), config_id, operation_id))
        conn.commit()
    return result


def refresh_native_operation(config_id, symbol, operation_id, previous, *, market_tool=None):
    """Only observe exact saved identities; never claim or submit during recovery."""
    from backend import database
    from backend.utils.trade_operations import refresh_operation_receipt
    from backend.config import config as runtime
    cfg = runtime.get_config_by_id(config_id) or dict(getattr(market_tool, 'runtime_config', {}) or {})
    with database.get_db_conn() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        native = conn.execute('SELECT * FROM unified_native_intents WHERE config_id=? AND operation_id=? AND symbol=?',
                              (config_id, operation_id, symbol)).fetchone() if 'unified_native_intents' in tables else None
        saved = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                             (config_id, operation_id)).fetchone()
        original = json.loads(saved[0]) if saved else {}
        context = original.get('trusted_context', {})
    cfg = {**context, **cfg}
    if not native and cfg.get('mode') != 'SPOT_DCA' and cfg.get('market_type') != 'spot':
        from backend.utils.exit_policy import effective_exit_mode
        if cfg.get('mode') == 'REAL' and effective_exit_mode(cfg) != 'independent_exits':
            return _refresh_attached_operation(config_id, symbol, operation_id, previous, market_tool=market_tool)
        if market_tool is not None:
            from backend.utils.independent_exits import IndependentExits
            from backend.utils.trade_recovery import recover_operation
            service = IndependentExits(market_tool)
            service.config_id = config_id
            return recover_operation(config_id, symbol, operation_id, service=service, read_exchange=True)
        return refresh_operation_receipt(config_id, symbol, operation_id, previous)
    result = dict(previous) if isinstance(previous, dict) else {'status': 'unknown', 'operation_id': operation_id}
    arguments = original.get('arguments', {})
    result.setdefault('action', original.get('action'))
    if arguments.get('side'):
        result.setdefault('side', arguments['side'])
    if original.get('action') in {'edit', 'cancel'} and arguments.get('order_id'):
        result.setdefault('order_id', arguments['order_id'])
    try:
        from backend.agent.agent_tools import MarketTool
        mt = market_tool or MarketTool(config_id=config_id)
        if native:
            from backend.utils.execution_ledger import account_scope
            if native['account_scope'] != account_scope(mt.exchange, config_id):
                raise ValueError('Original operation belongs to different exchange credentials')
            intent = json.loads(native['request_json'])
            if intent.get('action', {}).get('action') == 'protect':
                return _refresh_attached_operation(config_id, symbol, operation_id, result, native_intent=intent, market_tool=mt)
            saved = json.loads(native['result']) if native['result'] else {}
            order_id = saved.get('id') or result.get('exchange_order_id')
            order = mt.exchange.fetch_order(str(order_id or native['client_id']), symbol,
                {} if order_id else {'clientOrderId': native['client_id']})
            if (not order.get('id') or order_id and str(order['id']) != str(order_id) or
                    not order_id and order.get('clientOrderId') != native['client_id']):
                raise ValueError('Original client order identity is not verified')
            from backend.utils.trade_operations import _same_symbol
            if order.get('symbol') and not _same_symbol(order['symbol'], symbol):
                raise ValueError('Original order market is not verified')
            from backend.agent.trading_contracts import UnifiedCloseAction
            action = UnifiedCloseAction.model_validate(intent['action'])
            updated = _native_receipt(order, action, operation_id, symbol, contract_size=intent['contract_size'])
            if updated.get('order_status') in {'filled', 'canceled'} and updated.get('filled_amount') is None:
                updated.update(status='pending', error_code='fill_unverified')
            if result.get('status') == 'completed' and updated.get('status') == 'pending':
                # Missing current evidence never erases an original verified fill.
                for key in ('status', 'filled_amount', 'filled', 'resolved_amount'):
                    if key in result:
                        updated[key] = result[key]
            result.update(updated)
        else:
            if original.get('action') in {'edit', 'cancel'}:
                from backend.utils.spot_management import recover_spot_action
                recovered = recover_spot_action(mt, config_id, symbol, operation_id, arguments, result)
                if recovered is not None:
                    return {**recovered, 'contract_version': 3, 'receipt_version': 1,
                            'operation_id': operation_id, 'action': original['action'], 'symbol': symbol}
            from backend.mcp.spot_orders import _rows, _refresh, _receipt
            row = next((row for row in _rows(config_id, symbol) if row['operation_id'] == operation_id or
                        row['exit_id'] == result.get('exit_id')), None)
            if row:
                _refresh(mt.exchange, row)
                observed = _receipt(row)
                result.update(observed, order_status=observed['status'], filled_amount=observed['filled'])
                result['status'] = 'submitted' if observed['status'] == 'open' else tool_result_status(observed)
            elif result.get('order_id') or result.get('client_id'):
                target = _raw_order_id(result['order_id']) if result.get('order_id') else None
                observed = mt.exchange.fetch_order(target or result['client_id'], symbol,
                                                   {} if target else {'clientOrderId': result['client_id']})
                if not observed.get('id') or target and str(observed.get('id')) != target or not target and observed.get('clientOrderId') != result['client_id'] or observed.get('symbol') not in {None, symbol}:
                    raise ValueError('Original spot order identity is not verified')
                result.update(order_id='regular:' + str(observed['id']), order_status=observed.get('status'), filled_amount=observed.get('filled'),
                              remaining_amount=observed.get('remaining'))
                if result.get('action') in {'open', 'close'}:
                    result['status'] = 'pending' if observed.get('filled') is None else 'submitted' if observed.get('status') == 'open' else tool_result_status(observed)
                elif observed.get('filled') is None:
                    result['status'] = 'pending'
            else:
                raise ValueError('Original spot order identity is unavailable')
        result['query'] = {'status': 'completed', 'queried_at': time.time(), 'source': 'exchange_order'}
    except Exception as exc:
        result['query'] = {'status': 'failed', 'queried_at': time.time(), 'observed_at': result.get('verified_at'), 'errors': [str(exc)]}
    result.update(contract_version=3, receipt_version=1, operation_id=operation_id)
    return result


def _observe_attached_order(service, record, symbol, trigger):
    """Verify the saved target before accepting the query's mutable projection."""
    from backend.utils.trade_operations import _same_symbol
    identity, client_id = record.get('id'), record.get('client_id')
    order = service._query(record, symbol, trigger)
    if not order:
        return None
    info = order.get('info') or {}
    observed_client = order.get('clientOrderId') or info.get('clientAlgoId') or info.get('clOrdId')
    if (identity is not None and str(order.get('id')) != str(identity) or
            identity is None and (not client_id or observed_client != client_id)):
        raise ValueError('Original attached order identity is not verified')
    if order.get('symbol') and not _same_symbol(order['symbol'], symbol):
        raise ValueError('Original attached order market is not verified')
    return order


def _refresh_attached_operation(config_id, symbol, operation_id, previous, *, native_intent=None, market_tool=None):
    from backend import database
    from backend.agent.agent_tools import MarketTool
    from backend.utils.position_protection import PositionProtection
    result = dict(previous) if isinstance(previous, dict) else {'status': 'unknown'}
    try:
        with database.get_db_conn() as conn:
            row = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                               (config_id, operation_id)).fetchone()
        intent = json.loads(row[0]) if row else {}
        action, args = intent.get('action'), intent.get('arguments', {})
        service = PositionProtection(market_tool or MarketTool(config_id=config_id))
        service.config_id = config_id
        market = service._market(symbol)
        size = float(market.get('contractSize') or 1)
        observed, cleanup = [], []
        for side in ('LONG', 'SHORT'):
            if args.get('side') and side != args['side']:
                continue
            plan = service._load(market['symbol'], side)
            stored_owner = config_id
            if not plan and market_tool is not None:
                service.config_id = market_tool.config_id
                accepted_plan = service._load(market['symbol'], side)
                service.config_id = config_id
                if accepted_plan and accepted_plan.get('original_owner') == config_id:
                    plan = accepted_plan
                    stored_owner = market_tool.config_id
            if not plan or plan.get('account_scope') != service.account_scope:
                continue
            if action == 'protect':
                if not native_intent or plan.get('revision') != native_intent.get('expected_revision', -2) + 1:
                    continue
                if any(args.get(key) is not None and args[key] != plan.get(key) for key in ('stop_loss', 'take_profit')):
                    continue
                targets = [('algo', record) for record in plan.get('legs', []) if
                           record.get('trigger_price') == plan.get('stop_loss' if record.get('kind') == 'sl' else 'take_profit') and
                           record.get('status') not in {'canceled', 'cancelled', 'rejected'}]
                for record in plan.get('legs', []):
                    if record.get('trigger_price') == plan.get('stop_loss' if record.get('kind') == 'sl' else 'take_profit'):
                        continue
                    if record.get('status') not in {'closed', 'canceled', 'cancelled', 'rejected', 'expired'}:
                        order = _observe_attached_order(service, record, market['symbol'], True)
                        if not order or order.get('status') not in {'closed', 'canceled', 'cancelled', 'rejected', 'expired'} or order.get('filled') is None:
                            cleanup.append('algo:' + str(record.get('id') or record.get('client_id')))
            else:
                target = _raw_order_id(args.get('order_id', ''))
                records = [(channel, record) for channel, records in
                           (('regular', plan.get('entries', [])), ('algo', plan.get('legs', []))) for record in records]
                targets = [(channel, record) for channel, record in records if record.get('operation_id') == operation_id] if action == 'open' else [
                    (channel, record) for channel, record in records if str(record.get('id')) == target and
                    (not str(args.get('order_id', '')).startswith(('regular:', 'algo:')) or args['order_id'].startswith(channel + ':'))]
            for channel, record in targets:
                order = _observe_attached_order(service, record, market['symbol'], channel == 'algo')
                if not order:
                    raise ValueError('Original attached order identity is not verified')
                record['filled'] = order.get('filled')
                observed.append((channel, record, order))
                result['side'] = plan['side']
            # Persist observations only. _save/_reconcile would perform ownership
            # or financial maintenance, neither belongs in this read-only path.
            if targets:
                expected = plan.get('record_version', 0)
                plan['record_version'] = expected + 1
                with database.get_db_conn() as conn:
                    conn.execute('UPDATE real_protection_plans SET payload=? WHERE config_id=? AND symbol=? AND side=? '
                                 'AND COALESCE(json_extract(payload,\'$.record_version\'),0)=?',
                                 (json.dumps(plan), stored_owner, market['symbol'], side, expected))
                    conn.commit()
        if not observed:
            raise ValueError('Original attached operation evidence is unavailable')
        missing_fill = any(order.get('filled') is None for _, _, order in observed)
        if action == 'protect':
            result['order_ids'] = [channel + ':' + str(record['id']) for channel, record, _ in observed]
            result['status'] = 'pending' if missing_fill else 'completed' if all(record.get('status') == 'open' for _, record, _ in observed) else 'unknown'
            if cleanup:
                result['cleanup'] = {'status': 'pending', 'blocking_order_ids': cleanup,
                                     'error_code': 'old_protection_cleanup_pending'}
        else:
            channel, record, order = observed[0]
            result.update(order_id=channel + ':' + str(record['id']), order_status=order.get('status'),
                          filled_amount=None if order.get('filled') is None else float(order['filled']) * size)
            if action == 'open':
                result['status'] = 'pending' if missing_fill else 'submitted' if order.get('status') == 'open' else tool_result_status(order)
            elif action == 'cancel':
                result['status'] = 'completed' if not missing_fill and order.get('status') in {'closed', 'canceled', 'cancelled', 'expired'} else 'pending'
            elif action == 'edit':
                result['status'] = 'completed' if (record.get('amendment') or {}).get('state') == 'confirmed' else 'pending'
        result['query'] = {'status': 'completed', 'queried_at': time.time(), 'source': 'exchange_order'}
    except Exception as exc:
        result['query'] = {'status': 'failed', 'queried_at': time.time(), 'errors': [str(exc)]}
    result.update(contract_version=3, receipt_version=1, operation_id=operation_id)
    return result


def _strategy_attached(action, config_id, symbol, operation_id, mt):
    from backend import database
    if action.action == 'close':
        price = float(mt.exchange.fetch_ticker(symbol)['last'])
        return database.reduce_mock_positions(config_id, symbol, action.side, action.amount or 0,
                                              price, action.reason, operation_id=operation_id)
    from backend.database_independent import initialize_independent_schema
    from backend.utils.position_protection import PositionProtection
    intent = json.dumps(action.model_dump(exclude_none=True), sort_keys=True)
    order_id = 'ST3-' + hashlib.sha256(f'{config_id}\0{operation_id}'.encode()).hexdigest()[:28]
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize_independent_schema(conn.cursor())
        saved = conn.execute('SELECT result,request_json FROM mock_trade_operations WHERE config_id=? AND symbol=? AND operation_id=?',
                             (config_id, symbol, operation_id)).fetchone()
        if saved:
            if saved['request_json'] != intent:
                raise ValueError('identity_conflict: simulated action intent changed')
            return json.loads(saved['result'])
        if action.action == 'open':
            conn.execute('INSERT OR IGNORE INTO mock_accounts(config_id,symbol,balance,failures) VALUES(?,?,10000,0)', (config_id, symbol))
            balance = float(conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (config_id,)).fetchone()[0])
            reserved = float(conn.execute("SELECT COALESCE(SUM(price*amount),0) FROM mock_orders WHERE config_id=? AND status='OPEN'", (config_id,)).fetchone()[0])
            if action.price * action.amount > max(0, balance - reserved):
                raise ValueError('Insufficient strategy balance for the requested entry')
            PositionProtection._validate(action.side, action.price, action.stop_loss, action.take_profit)
            conn.execute('''INSERT INTO mock_orders(order_id,timestamp,symbol,agent_name,config_id,side,type,price,amount,
                            stop_loss,take_profit,expire_at,status,is_filled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',0)''',
                         (order_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), symbol, config_id, config_id,
                          'BUY' if action.side == 'LONG' else 'SELL', 'limit', action.price, action.amount,
                          action.stop_loss, action.take_profit, time.time() + (action.valid_duration_hours or 24) * 3600))
            result = {'status': 'submitted', 'order_status': 'open', 'order_id': 'regular:' + order_id,
                      'side': action.side, 'amount': action.amount, 'remaining_amount': action.amount,
                      'filled_amount': 0, 'price': action.price}
        else:
            if str(action.order_id).startswith('algo:'):
                raise ValueError('Attached strategy orders use the regular channel')
            identity = _raw_order_id(action.order_id)
            row = conn.execute('SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=?',
                               (identity, config_id, symbol)).fetchone()
            if not row:
                raise ValueError('Order is not in this task and symbol')
            side = 'LONG' if str(row['side']).upper() == 'BUY' else 'SHORT'
            if action.action == 'cancel':
                if row['is_filled'] and row['status'] == 'OPEN':
                    raise ValueError('A filled position cannot be canceled; use close')
                if row['status'] == 'OPEN':
                    conn.execute("UPDATE mock_orders SET status='CANCELLED' WHERE order_id=?", (identity,))
                state = 'canceled' if row['status'] in {'OPEN', 'CANCELLED'} else 'filled' if row['is_filled'] else row['status'].lower()
                result = {'status': 'completed', 'order_status': state, 'order_id': action.order_id}
            elif action.action == 'edit':
                if row['status'] != 'OPEN' or row['is_filled'] or row['expire_at'] is not None and row['expire_at'] <= time.time():
                    raise ValueError('Only a current unfilled entry can be edited')
                price = action.price if action.price is not None else row['price']
                amount = action.remaining_amount if action.remaining_amount is not None else row['amount']
                sl = action.stop_loss if action.stop_loss is not None else row['stop_loss']
                tp = action.take_profit if action.take_profit is not None else row['take_profit']
                PositionProtection._validate(side, price, sl, tp)
                balance_row = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (config_id,)).fetchone()
                reserved = conn.execute("SELECT COALESCE(SUM(price*amount),0) FROM mock_orders WHERE config_id=? AND status='OPEN' AND order_id!=?", (config_id, identity)).fetchone()[0]
                if not balance_row or price * amount > max(0, float(balance_row[0]) - float(reserved)):
                    raise ValueError('Edited entry exceeds available strategy balance')
                conn.execute('UPDATE mock_orders SET price=?,amount=?,stop_loss=?,take_profit=? WHERE order_id=?', (price, amount, sl, tp, identity))
                result = {'status': 'completed', 'order_status': 'open', 'order_id': action.order_id,
                          'price': price, 'remaining_amount': amount, 'edit_method': 'native'}
            else:
                if row['status'] != 'OPEN' or side != action.side:
                    raise ValueError('Protection target has ended or its side differs')
                sl = action.stop_loss if action.stop_loss is not None else row['stop_loss']
                tp = action.take_profit if action.take_profit is not None else row['take_profit']
                PositionProtection._validate(side, row['price'], sl, tp)
                conn.execute('UPDATE mock_orders SET stop_loss=?,take_profit=? WHERE order_id=?', (sl, tp, identity))
                result = {'status': 'completed', 'order_id': action.order_id, 'side': side, 'stop_loss': sl, 'take_profit': tp}
        result.update(contract_version=3, receipt_version=1, operation_id=operation_id, action=action.action, symbol=symbol)
        conn.execute('INSERT INTO mock_trade_operations(config_id,symbol,operation_id,result,request_json) VALUES(?,?,?,?,?)',
                     (config_id, symbol, operation_id, json.dumps(result), intent))
        conn.commit()
    if action.action == 'open':
        try:
            database.save_order_log(order_id, symbol, config_id, 'BUY' if action.side == 'LONG' else 'SELL',
                                    action.price, action.take_profit or 0, action.stop_loss or 0, action.reason,
                                    trade_mode='STRATEGY', config_id=config_id, amount=action.amount, event_type='ORDER_CREATED')
        except Exception as exc:
            result['warnings'] = [{'reason_code': 'audit_write_failed', 'error': str(exc)}]
    return result


def _attached_real(action, config_id, symbol, cfg, operation_id, mt):
    from backend.utils.position_protection import PositionProtection, TERMINAL
    from backend.agent.agent_models import OpenOrderReal, CloseOrder
    service = PositionProtection(mt)
    market = service._market(symbol)
    size = float(market.get('contractSize') or 1)
    if action.action == 'open':
        op = OpenOrderReal(action='BUY_LIMIT' if action.side == 'LONG' else 'SELL_LIMIT',
                           amount=action.amount, entry_price=action.price, reason=action.reason,
                           stop_loss=action.stop_loss, take_profit=action.take_profit)
        try:
            result = service.open(symbol, op, operation_id=operation_id)
        except Exception as exc:
            plan = service._load(market['symbol'], action.side) or {}
            record = next((entry for entry in plan.get('entries', []) if entry.get('operation_id') == operation_id), None)
            if not record:
                raise
            rejected = record.get('status') == 'rejected'
            return {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id,
                    'status': 'failed' if rejected else 'unknown', 'phase': 'submit',
                    'submission_state': 'rejected' if rejected else 'unknown',
                    'client_id': record['client_id'], 'order_id': 'regular:' + str(record['id']) if record.get('id') else None,
                    'error': str(exc), 'error_code': type(exc).__name__, 'side': action.side,
                    'protection_state': plan.get('state'), 'stop_loss': plan.get('stop_loss'), 'take_profit': plan.get('take_profit'),
                    'protection_error': plan.get('error'),
                    'recovery': {'state': 'background', 'target_operation_id': operation_id}}
        return _native_receipt(result, action, operation_id, symbol, contract_size=size)
    if action.action == 'protect':
        from backend import database
        before = service._load(market['symbol'], action.side)
        expected = (before or {}).get('revision', 0)
        intent = {'action': action.model_dump(exclude_none=True), 'expected_revision': expected}
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            _native_table(conn)
            conn.execute('INSERT INTO unified_native_intents VALUES(?,?,?,?,?,?,NULL)',
                         (config_id, operation_id, symbol, service.account_scope, '', json.dumps(intent)))
            conn.commit()
        plan = service.adjust(symbol, action.side, sl=action.stop_loss, tp=action.take_profit, expected_revision=expected)
        from backend.utils.trade_operations import financial_ack_only
        live_legs = [item for item in plan.get('legs', []) if item.get('id') and item.get('status') not in TERMINAL]
        result = {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id, 'action': 'protect',
                'status': 'pending' if plan.get('error') else 'submitted' if financial_ack_only.get() and live_legs else 'completed', 'side': action.side,
                'protection_state': plan['state'], 'stop_loss': plan['stop_loss'], 'take_profit': plan['take_profit'],
                'order_ids': ['algo:' + str(item['id']) for item in live_legs],
                **({'order_status': 'waiting_trigger', 'quantity_mode': 'all',
                    'filled_amount': 0 if all(type((item.get('acknowledgement') or {}).get('filled')) in {int, float}
                                              and item['acknowledgement']['filled'] == 0 for item in live_legs) else None}
                   if financial_ack_only.get() and live_legs else {}),
                **({'error': plan['error']} if plan.get('error') else {})}
        with database.get_db_conn() as conn:
            conn.execute('UPDATE unified_native_intents SET result=? WHERE config_id=? AND operation_id=?',
                         (json.dumps(result), config_id, operation_id))
            conn.commit()
        return result
    if action.action == 'close':
        result = _attached_close(action, config_id, symbol, operation_id, mt, service)
        receipt = _native_receipt(result, action, operation_id, symbol, contract_size=size)
        if isinstance(result, dict) and result.get('id'):
            from backend.utils.execution_ledger import register_order
            try:
                register_order(service.account_scope, market['symbol'], str(result['id']), config_id,
                               'agent_exit', reason=action.reason, side=action.side, operation_id=operation_id)
            except Exception as exc:
                receipt['warnings'] = [{'reason_code': 'audit_write_failed', 'error': str(exc)}]
        return receipt
    identity = _raw_order_id(action.order_id)
    if action.action == 'edit':
        if str(action.order_id).startswith('algo:'):
            raise ValueError('Attached entry edit requires the regular channel')
        if action.trigger_price is not None:
            raise ValueError('Attached entry edit does not accept trigger_price')
        result = service.amend_entry(symbol, identity, price=action.price, reason=action.reason,
                                     remaining_amount=action.remaining_amount, operation_id=operation_id)
        receipt = _native_receipt(result, action, operation_id, symbol, contract_size=size)
        receipt['status'] = {'confirmed': 'completed', 'unchanged': 'completed', 'pending': 'pending', 'rejected': 'failed'}.get(result.get('amendment_state'), receipt['status'])
        receipt['edit_method'] = 'native'
        return receipt
    from backend.utils.exit_orders import parse_order_reference
    channel, _ = parse_order_reference(action.order_id)
    for side in ('LONG', 'SHORT'):
        plan = service._load(market['symbol'], side)
        if not plan:
            continue
        for kind, records in (('regular', plan.get('entries', [])), ('algo', plan.get('legs', []))):
            for record in records:
                if str(record.get('id')) != identity or channel and channel != kind:
                    continue
                record['cancel_operation_id'] = operation_id
                service._save(plan)
                service._cancel(record, plan, trigger=kind == 'algo')
                result = _native_receipt(record, action, operation_id, symbol, contract_size=size)
                result['order_id'] = action.order_id
                result['side'] = side
                result['status'] = 'pending' if record.get('fill_pending') or record.get('status') not in TERMINAL else 'completed'
                return result
    raise ValueError('Cannot verify the current managed cancellation target')


def dispatch_live_action(action, config_id, symbol, config, operation_id):
    from backend.agent.agent_tools import MarketTool
    from backend.utils.exit_policy import effective_exit_mode
    from backend.utils.position_management import management_action
    from backend.config import config as runtime
    current = runtime.get_config_by_id(config_id)
    if not current:
        return {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id, 'status': 'failed',
                'phase': 'preflight', 'submission_state': 'not_submitted', 'submitted': False,
                'error_code': 'configuration_unavailable', 'error': 'Trading configuration is missing or has been deleted'}
    for key in ('mode', 'market_type', 'exit_mode', 'exchange_profile_id'):
        if current.get(key) != config.get(key):
            return {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id, 'status': 'failed',
                    'phase': 'preflight', 'error_code': 'configuration_changed', 'error': 'Trading configuration changed; analyze again'}
    mt = MarketTool(config_id=config_id)
    mode = config.get('mode', 'STRATEGY').upper()
    spot = mode == 'SPOT_DCA' or config.get('market_type') == 'spot'
    context = management_action(mt, symbol, getattr(action, 'side', None), operation_id,
                                automatic=True, order_id=getattr(action, 'order_id', None)) if mode != 'STRATEGY' else nullcontext(None)
    try:
        if mode != 'STRATEGY' and action.action == 'open':
            from backend.mcp.guard import assert_entry_allowed
            assert_entry_allowed(mt, symbol, side=action.side, only_capability=True)
        with context as claim:
            if spot:
                from backend.mcp.spot_orders import execute_spot_action
                result = execute_spot_action(config_id, symbol, action.model_dump(exclude_none=True), operation_id)
            elif effective_exit_mode(config) == 'independent_exits':
                result = _independent(action, config_id, symbol, config, operation_id, mt)
            elif mode == 'STRATEGY':
                result = _strategy_attached(action, config_id, symbol, operation_id, mt)
            else:
                result = _attached_real(action, config_id, symbol, config, operation_id, mt)
            result = _native_receipt(result, action, operation_id, symbol)
            if claim is not None:
                claim['outcome_status'] = tool_result_status(result)
            return result
    except ValueError as exc:
        attempted = mode == 'REAL' and not spot and effective_exit_mode(config) != 'independent_exits' and _attached_write_started(config_id, operation_id)
        return {'contract_version': 3, 'receipt_version': 1, 'operation_id': operation_id,
                'status': 'unknown' if attempted else 'failed', 'phase': 'verify' if attempted else 'preflight',
                'submission_state': 'unknown' if attempted else 'not_submitted',
                'submitted': None if attempted else False,
                'error_code': getattr(exc, 'code', 'admission_rejected'), 'error': str(exc),
                **({'recovery': {'state': 'background', 'target_operation_id': operation_id}} if attempted else {})}


def _attached_write_started(config_id, operation_id):
    from backend import database
    with database.get_db_conn() as conn:
        plans = [json.loads(row[0]) for row in conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=?', (config_id,))]
        if any(record.get('operation_id') == operation_id or record.get('cancel_operation_id') == operation_id or
               (record.get('amendment') or {}).get('operation_id') == operation_id
               for plan in plans for record in plan.get('entries', []) + plan.get('legs', [])):
            return True
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        native = conn.execute('SELECT request_json,result FROM unified_native_intents WHERE config_id=? AND operation_id=?',
                              (config_id, operation_id)).fetchone() if 'unified_native_intents' in tables else None
    if native:
        intent = json.loads(native['request_json'])
        if intent.get('action', {}).get('action') == 'close':
            return True
        return any(plan.get('side') == intent['action'].get('side') and plan.get('revision', 0) > intent.get('expected_revision', 0) for plan in plans)
    return False


def live_account_summary(config_id, parsed, config):
    if config.get('mode') == 'STRATEGY':
        from backend import database
        from backend.database_independent import MockIndependentTrading
        symbols = list(dict.fromkeys(action.symbol for action in parsed.actions))
        with database.get_db_conn() as conn:
            account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (config_id,)).fetchone()
            attached = [dict(row) for row in conn.execute("SELECT * FROM mock_orders WHERE config_id=? AND is_filled=1 AND status='OPEN'", (config_id,))]
        positions = attached + [position for symbol in symbols for position in MockIndependentTrading(config_id, symbol).snapshot()['positions']]
        return {'status': 'completed' if account else 'unavailable', 'symbols': symbols,
                'balance': account[0] if account else None, 'positions': positions,
                'observed_at': None, 'source': 'local_ledger', 'generated_at': time.time()}
    from backend.agent.agent_tools import MarketTool
    mt = MarketTool(config_id=config_id)
    symbols = list(dict.fromkeys(action.symbol for action in parsed.actions))
    accounts = {symbol: mt.get_account_status(symbol, is_real=config.get('mode') != 'STRATEGY',
                                             agent_name=config_id, config_id=config_id) for symbol in symbols}
    evidence = {symbol: account.get('evidence_state', {}) for symbol, account in accounts.items()}
    observed = [item['observed_at'] for facets in evidence.values() for item in facets.values() if item.get('observed_at')]
    return {'status': 'failed' if any(account.get('error') for account in accounts.values()) else 'completed',
            'symbols': symbols, 'observed_at': min(observed) if observed else None, 'generated_at': time.time(),
            'evidence_state': evidence,
            'balances': {symbol: account.get('available_balance') for symbol, account in accounts.items()},
            'positions': [position for account in accounts.values() for position in account.get('real_positions', [])],
            'errors': {symbol: account['error'] for symbol, account in accounts.items() if account.get('error')}}
