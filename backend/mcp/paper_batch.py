"""Durable ordered paper requests; recovery never submits an unattempted tail."""
from __future__ import annotations

import contextvars
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from functools import wraps

from . import paper, paper_orders, store
from .resilience import remaining
from .paper_storage import decode, one, bounded

_step = contextvars.ContextVar('paper_batch_step', default=None)
_running = set()
_running_lock = threading.Lock()


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _load(conn, namespace, user_id, operation_id):
    from .paper_storage import max_payload, PaperPayloadLimit, blob_size, read_blob
    row = conn.execute('SELECT rowid FROM paper_batches WHERE namespace=? AND user_id=? AND operation_id=?',
                        (namespace, user_id, operation_id)).fetchone()
    if row and blob_size(conn, 'paper_batches', 'payload', row[0]) > max_payload():
        raise PaperPayloadLimit('paper_payload_limit: original parent requires bounded authorized audit; raw identity retained')
    if row is None:
        return None
    metadata = conn.execute('SELECT namespace,user_id,operation_id,fingerprint,worker_pid,worker_token,updated_at FROM paper_batches WHERE rowid=?',
                            (row[0],)).fetchone()
    return {**dict(metadata), 'payload': read_blob(conn, 'paper_batches', 'payload', row[0], identity='paper parent')}


def _save(conn, namespace, user_id, operation_id, payload):
    from .paper_storage import max_payload, PaperPayloadLimit
    encoded = _encode(payload)
    if len(encoded.encode('utf-8')) > max_payload():
        raise PaperPayloadLimit('paper_payload_limit: parent progress exceeds complete read budget')
    from .paper_storage import receipt_revision
    # Preserve the first available pre-upgrade observation before recovery.
    old = _load(conn, namespace, user_id, operation_id)
    if old is not None:
        receipt_revision(conn, namespace, user_id, operation_id, old['payload'])
    receipt_revision(conn, namespace, user_id, operation_id, encoded)
    conn.execute('UPDATE paper_batches SET payload=?,updated_at=? WHERE namespace=? AND user_id=? AND operation_id=?',
                 (encoded, time.time(), namespace, user_id, operation_id))
    conn.execute('INSERT INTO paper_batch_status VALUES(?,?,?,?) ON CONFLICT(namespace,user_id,operation_id) DO UPDATE SET status=excluded.status',
                 (namespace, user_id, operation_id, payload['status']))


def _worker_active(row):
    if row['worker_pid'] == os.getpid():
        with _running_lock:
            if row['worker_token'] in _running:
                return True
    from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
    try:
        with scope_file_lock('paper-batch', row['namespace'] + '\0' + row['user_id'], row['operation_id']):
            return False
    except ManagementLockBusy:
        return True


def _serialized_batch(function):
    @wraps(function)
    def wrapped(caller, user_id, request, operation_id):
        namespace = paper.require(caller, 'trade')
        paper.require(caller, 'read')
        paper.identity(user_id)
        if not isinstance(operation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
            raise ValueError('operation_id must be a stable 8–160 character identifier')
        from backend.utils.management_lock import scope_file_lock, ManagementLockBusy
        try:
            with scope_file_lock('paper-batch', namespace + '\0' + user_id, operation_id):
                return function(caller, user_id, request, operation_id)
        except ManagementLockBusy:
            storage_opened = False
            try:
                with store.connection(recovery=True) as conn:
                    storage_opened = True
                    row = _load(conn, namespace, user_id, operation_id)
                    if row:
                        if row['fingerprint'] != store.digest(_encode(request)):
                            raise ValueError('operation_id was already used for a different paper batch')
                        return _recover(conn, row, active=True)
            except Exception as exc:
                if storage_opened and not isinstance(exc, sqlite3.Error):
                    raise
                return _intent_storage_failure(operation_id, user_id)
            return {'operation_id': operation_id, 'status': 'pending', 'contract_version': 3,
                    'recovery': {'status': 'system_updates', 'operation_id': operation_id, 'user_id': user_id}}
    return wrapped


def _action_receipt(raw, action):
    # The immutable atomic child owns the full account evidence. Repeating it
    # in every parent step grows quadratically during progress updates.
    result = {key: value for key, value in raw.items() if key != 'account'}
    order = raw.get('order') or {}
    fill = raw.get('fill') or order.get('fill') or {}
    if order:
        result.update(order_id=order.get('order_id'),
                      order_status='waiting_trigger' if order.get('state') == 'open' and order.get('order_type') in {'stop_loss', 'take_profit'} else order.get('state'),
                      quantity_mode='all' if order.get('close_all') else 'fixed', amount=order.get('amount'))
        if order.get('state') == 'open' and action in {'open', 'close'}:
            result['status'] = 'submitted'
    if fill:
        result.update(order_status='filled', filled_amount=fill.get('amount'), amount=fill.get('amount'))
    if order or fill or raw.get('orders'):
        result['quantity_unit'] = 'base'
    return result


def record_child_commit(conn, receipt):
    """Called inside the existing single-action account transaction."""
    step = _step.get()
    if step is None:
        return
    namespace, user_id, parent_id, index, action = step
    row = _load(conn, namespace, user_id, parent_id)
    payload = decode(row['payload'], identity='paper parent progress')
    item = payload['results'][index]
    item.update(_action_receipt(receipt, action), index=index, action=action,
                operation_id=receipt['operation_id'], attempted=True)
    _save(conn, namespace, user_id, parent_id, payload)


def _status(items):
    values = {item['status'] for item in items}
    for status in ('unknown', 'pending', 'failed'):
        if status in values:
            return status
    if 'not_executed' in values:
        return 'failed'
    return 'submitted' if 'submitted' in values else 'completed'


def _intent_storage_failure(operation_id, user_id):
    # No financial callback has started in this invocation. An unreadable
    # original ledger can still contain effects from a previous invocation,
    # so preserve uncertainty instead of asserting that the parent failed.
    return {'operation_id': operation_id, 'contract_version': 3, 'paper': True,
            'status': 'unknown', 'error_code': 'intent_storage_unavailable',
            'error': 'The original parent intent could not be read or persisted.',
            'phase': 'persist_intent', 'submission_state': 'not_submitted', 'submitted': False,
            'message': 'This invocation dispatched no action. Prior parent effects could not be verified; original identity retained for system updates.',
            'query': {'status': 'failed', 'source': 'parent_intent_storage'},
            'recovery': {'status': 'system_updates', 'operation_id': operation_id, 'user_id': user_id}}


def _recover(conn, row, active=None):
    payload = decode(row['payload'], identity='original paper parent')
    was_running = payload['status'] in {'started', 'pending'}
    active = was_running and (_worker_active(row) if active is None else active)
    for item in payload['results']:
        receipt = one(conn, 'paper_operations', 'result', 'namespace=? AND user_id=? AND operation_id=?',
                      (row['namespace'], row['user_id'], item['operation_id']), identity='paper child evidence')
        if receipt:
            from .paper_storage import observe_receipt
            original = observe_receipt(conn, row['namespace'], row['user_id'], item['operation_id'], receipt)
            if original.get('order', {}).get('order_id'):
                current = one(conn, 'paper_orders', 'payload', 'namespace=? AND user_id=? AND order_id=?',
                              (row['namespace'], row['user_id'], original['order']['order_id']), identity='paper original order')
                if current:
                    original['order'] = current
                    if original['order']['state'] == 'filled':
                        original['status'] = 'completed'
            item.update(_action_receipt(original, item['action']), attempted=True)
        elif item['status'] == 'pending' and not active:
            # Account mutation and child receipt are one transaction. No receipt
            # after the worker has ended proves that this transaction did not commit.
            item.update(status='failed', error_code='worker_interrupted', submission='not_submitted')
    payload['status'] = 'pending' if active else 'failed' if payload.get('error') and not any(item.get('attempted') for item in payload['results']) else _status(payload['results'])
    if not active:
        for item in payload['results']:
            if was_running and not item.get('attempted'):
                item.update(status='not_executed', reason='Original worker ended; replay does not resume the tail')
        _save(conn, row['namespace'], row['user_id'], row['operation_id'], payload)
    return payload


def operation(caller, user_id, operation_id):
    namespace = paper.require(caller)
    paper.identity(user_id)
    with store.connection(recovery=True) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = _load(conn, namespace, user_id, operation_id)
        if row is None:
            child = conn.execute('SELECT parent_operation_id,action_index FROM paper_batch_children '
                                 'WHERE namespace=? AND user_id=? AND child_operation_id=?',
                                 (namespace, user_id, operation_id)).fetchone()
            if child is None:
                return None
            parent = _load(conn, namespace, user_id, child['parent_operation_id'])
            result = _recover(conn, parent)
            result = {**result['results'][child['action_index']], 'parent_operation_id': child['parent_operation_id']}
            conn.commit()
            return result
        result = _recover(conn, row)
        conn.commit()
    return result


def _dispatch(caller, user_id, action, child_id):
    args = action.model_dump(exclude_none=True) if hasattr(action, 'model_dump') else dict(action)
    kind = args.pop('action')
    args.pop('depends_on', None)
    args.pop('reason', None)
    symbol = args.pop('symbol', None)
    market = args.pop('market_type', None) or 'swap'
    exchange = args.pop('exchange', None)
    if kind == 'cancel':
        return paper_orders.cancel(caller, user_id, args['order_id'], child_id)
    if kind == 'edit':
        return paper_orders.edit_remaining(caller, user_id, args['order_id'], child_id,
                                           args.get('price'), args.get('trigger_price'), args.get('remaining_amount'))
    side = args.pop('side', 'LONG')
    if kind in {'close', 'protect'} and exchange is None:
        # Resolve an existing exposure before fetching a default-venue quote.
        # An exact order ID already binds cancel/edit to its venue; a new entry
        # may still use the configured default. Exit/protection intent needs
        # one unambiguous current holding, including after an earlier batch step.
        namespace = paper.require(caller)
        with store.connection() as conn:
            account = paper._get(conn, namespace, user_id)
            _, target = paper_orders.exposure(account, market, paper_orders.canonical(symbol, market),
                                               side if market == 'swap' else None)
        if target is not None:
            exchange = target['exchange']
    if kind == 'protect':
        return paper_orders.protection(caller, user_id, market, symbol, side, child_id,
                                      args.get('stop_loss'), args.get('take_profit'), args.get('amount'),
                                      args.get('close_all', True), exchange)
    trade_action = ('buy' if kind == 'open' else 'sell') if market == 'spot' else kind + '_' + side.lower()
    order_type = args.pop('order_type', None) or 'market'
    args.pop('order_id', None)
    args = {key: value for key, value in args.items() if key in {
        'amount', 'quote_amount', 'margin_amount', 'close_all', 'leverage', 'price',
        'trigger_price', 'stop_loss', 'take_profit'}}
    if args.get('leverage') is not None:
        if not float(args['leverage']).is_integer():
            raise ValueError('Paper leverage must be an integer')
        args['leverage'] = int(args['leverage'])
    if order_type in {'stop_loss', 'take_profit'} and args.get(order_type) is not None:
        args['trigger_price'] = args.pop(order_type)
    if order_type == 'market':
        args.pop('price', None)
        args.pop('trigger_price', None)
        return paper_orders.execute_market(caller, user_id, market, symbol, trade_action, child_id,
                                           exchange_name=exchange, **args)
    mapped_type = {'stop_market': 'stop_loss', 'take_profit_market': 'take_profit'}.get(order_type, order_type)
    return paper_orders.place(caller, user_id, market, symbol, trade_action, mapped_type, child_id,
                              exchange_name=exchange, **args)


@_serialized_batch
@bounded
def execute(caller, user_id, request, operation_id):
    paper.require(caller, 'read')
    namespace = paper.require(caller, 'trade')
    paper.identity(user_id)
    if not isinstance(operation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,160}', operation_id):
        raise ValueError('operation_id must be a stable 8–160 character identifier')
    fingerprint = store.digest(_encode(request))
    token = uuid.uuid4().hex
    with _running_lock:
        _running.add(token)
    try:
        storage_opened = False
        try:
            with store.connection() as conn:
                storage_opened = True
                conn.execute('BEGIN IMMEDIATE')
                previous = _load(conn, namespace, user_id, operation_id)
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        raise ValueError('operation_id was already used for a different paper batch')
                    result = _recover(conn, previous, active=False)
                    conn.commit()
                    return result
                legacy = conn.execute('SELECT 1 FROM paper_operations WHERE namespace=? AND user_id=? AND operation_id=?',
                                      (namespace, user_id, operation_id)).fetchone()
                if legacy:
                    raise ValueError('operation_id already belongs to a legacy paper request')
                if conn.execute('SELECT 1 FROM paper_batch_children WHERE namespace=? AND user_id=? AND child_operation_id=?',
                                (namespace, user_id, operation_id)).fetchone():
                    raise ValueError('operation_id already belongs to a reserved paper batch child')
                actions = request.get('actions') if isinstance(request.get('actions'), list) else []
                items = [{'index': index, 'action': action.get('action') if isinstance(action, dict) else None,
                          'operation_id': 'paper-child:' + store.digest('\0'.join((namespace, user_id, operation_id, str(index)))),
                          'status': 'not_executed', 'attempted': False} for index, action in enumerate(actions)]
                payload = {'operation_id': operation_id, 'contract_version': 3, 'paper': True, 'status': 'started',
                           'request': request, 'results': items,
                           'recovery': {'status': 'system_updates', 'operation_id': operation_id, 'user_id': user_id}}
                conn.execute('INSERT INTO paper_batches VALUES(?,?,?,?,?,?,?,?)',
                             (namespace, user_id, operation_id, fingerprint, _encode(payload), os.getpid(), token, time.time()))
                conn.executemany('INSERT INTO paper_batch_children VALUES(?,?,?,?,?)',
                                 [(namespace, user_id, item['operation_id'], operation_id, item['index']) for item in items])
                conn.commit()
        except Exception as exc:
            if storage_opened and not isinstance(exc, sqlite3.Error):
                raise  # Intent/identity conflicts retain their original contract.
            return _intent_storage_failure(operation_id, user_id)
        try:
            from backend.agent.unified_trading import validate_for_config
            account = paper.get_account(caller, user_id)
            config = {**paper.settings(), **account, 'mode': 'PAPER', 'market_type': 'paper', 'exit_mode': 'paper'}
            parsed = validate_for_config(request, config, engine='paper')
            from .paper_policy import preflight_actions
            with store.connection() as conn:
                preflight_actions(conn, paper._get(conn, namespace, user_id), parsed.actions)
            policy = parsed.execution_policy or 'stop_on_error'
            halted = False
            for index, action in enumerate(parsed.actions):
                item = items[index]
                dependencies = getattr(action, 'depends_on', None) or []
                blocked = [dep for dep in dependencies if items[dep]['status'] != 'completed']
                if halted or blocked:
                    item.update(reason_code='stopped_after_error' if halted else 'dependency_not_completed',
                                blocked_by_indices=blocked, blocked_by=blocked)
                    continue
                remaining('paper_action')
                item.update(status='pending', attempted=True)
                with store.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    _save(conn, namespace, user_id, operation_id, payload)
                    conn.commit()
                step_token = _step.set((namespace, user_id, operation_id, index, item['action']))
                try:
                    result = _dispatch(caller, user_id, action, item['operation_id'])
                    item.update(_action_receipt(result, item['action']), index=index, action=item['action'], attempted=True)
                except Exception as exc:
                    # Any account mutation has an atomic child receipt. Recover
                    # that receipt before classifying a post-commit exception.
                    try:
                        with store.connection(recovery=True) as conn:
                            saved = one(conn, 'paper_operations', 'result', 'namespace=? AND user_id=? AND operation_id=?',
                                        (namespace, user_id, item['operation_id']), identity='committed paper child')
                        if saved:
                            item.update(_action_receipt(saved, item['action']))
                        else:
                            item.update(status='failed', error=str(exc), submission='not_submitted')
                    except Exception:
                        item.update(status='unknown', error=str(exc), error_code='commit_evidence_unavailable',
                                    recovery=payload['recovery'])
                finally:
                    _step.reset(step_token)
                halted = policy == 'stop_on_error' and item['status'] in {'failed', 'pending', 'unknown'}
            payload['status'] = _status(items)
            if 'account' in (getattr(parsed, 'include', None) or []):
                try:
                    payload['account_summary'] = paper.get_account(caller, user_id)
                except Exception as exc:
                    payload['account_summary'] = {'status': 'failed', 'error': str(exc)}
        except Exception as exc:
            payload.update(status=_status(items) if any(item['attempted'] for item in items) else 'failed', error=str(exc))
        try:
            with store.connection(recovery=True) as conn:
                conn.execute('BEGIN IMMEDIATE')
                _save(conn, namespace, user_id, operation_id, payload)
                conn.commit()
        except Exception:
            payload['persistence_pending'] = True
        return payload
    finally:
        with _running_lock:
            _running.discard(token)


@bounded(critical=True)
def recover_pending():
    """Refresh only original local evidence; no financial callback or quote."""
    from .paper_storage import batch_size, max_payload, PaperPayloadLimit, blob_size
    with store.connection(recovery=True) as conn:
        saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='batch-recovery'").fetchone()
        cursor = saved[0] if saved else 0
        records = conn.execute('SELECT b.rowid,b.namespace,b.user_id,b.operation_id '
            'FROM paper_batches b LEFT JOIN paper_batch_status s ON s.namespace=b.namespace AND s.user_id=b.user_id AND s.operation_id=b.operation_id '
            "WHERE b.rowid>? AND (s.status IS NULL OR s.status IN ('started','pending','submitted','unknown')) ORDER BY b.rowid LIMIT ?",
            (cursor, batch_size() + 1)).fetchall()
        skipped, refreshed = [], 0
        for record in records[:batch_size()]:
            if blob_size(conn, 'paper_batches', 'payload', record['rowid']) > max_payload():
                skipped.append(record['rowid'])
                conn.execute('INSERT INTO paper_batch_status VALUES(?,?,?,?) ON CONFLICT(namespace,user_id,operation_id) DO UPDATE SET status=excluded.status',
                             (record['namespace'], record['user_id'], record['operation_id'], 'audit_only'))
                continue
            row = _load(conn, record['namespace'], record['user_id'], record['operation_id'])
            try:
                _recover(conn, row)
                refreshed += 1
            except PaperPayloadLimit:
                skipped.append(record['rowid'])
        more = len(records) > batch_size()
        next_cursor = records[min(len(records), batch_size()) - 1]['rowid'] if records else cursor
        conn.execute('INSERT INTO paper_scan_progress VALUES(?,?,?) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor,payload=excluded.payload',
                     ('batch-recovery', next_cursor if more else 0, '{}'))
        conn.commit()
    return {'refreshed': refreshed, 'skipped_records': skipped, 'scan_complete': not more, 'next_cursor': next_cursor if more else None}


operation = bounded(operation, critical=True)
