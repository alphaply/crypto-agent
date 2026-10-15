"""Bounded ledger reads. A partial ledger never authorizes a financial mutation."""
from __future__ import annotations

import base64
import json
import os
from contextlib import nullcontext
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps


class PaperPayloadLimit(ValueError):
    code = 'paper_payload_limit'


class _ReservedBlob(bytes):
    """The complete decoded value is already charged to the active lifetime."""


def limit(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ValueError(name + ' must be a positive integer') from None
    if value <= 0 or value > 1 << 30:
        raise ValueError(name + ' must be within 1..1073741824')
    return value


def max_payload():
    return limit('PAPER_MAX_PAYLOAD_BYTES', 262144)


def batch_size():
    return limit('PAPER_SCAN_BATCH_SIZE', 64)


def history_limit():
    return limit('PAPER_HISTORY_SCAN_LIMIT', 256)


_payload_lifetime = ContextVar('paper_payload_lifetime', default=None)


@contextmanager
def transient_payloads():
    """A helper returns compact facts, releasing its decoded inputs on return."""
    with ExitStack() as stack:
        token = _payload_lifetime.set(stack)
        try:
            yield
        finally:
            _payload_lifetime.reset(token)


def _reserve(size):
    from backend.utils.runtime_capacity import reserve_bytes
    return reserve_bytes('paper_payload', size * 4, timeout=0)


@contextmanager
def payload_scope(*, critical=False):
    """Keep decoded account/receipt commitments until the worker is finished."""
    from backend.utils.runtime_capacity import capacity_scope
    with capacity_scope('paper', critical=critical, timeout=0):
        if _payload_lifetime.get() is not None:
            yield
            return
        with ExitStack() as stack:
            token = _payload_lifetime.set(stack)
            try:
                yield
            finally:
                _payload_lifetime.reset(token)


def bounded(function=None, *, critical=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with payload_scope(critical=critical):
                return fn(*args, **kwargs)
        return wrapped
    return decorate(function) if function is not None else decorate


def decode(value, *, identity='ledger'):
    size = len(value.encode('utf-8')) if isinstance(value, str) else len(value)
    stack = _payload_lifetime.get()
    if isinstance(value, _ReservedBlob):
        return json.loads(value)
    if stack is not None:
        stack.enter_context(_reserve(size))
        return json.loads(value)
    with _reserve(size):
        return json.loads(value)


def blob_size(conn, table, column, rowid):
    from backend.utils.sqlite_blob import blob_size as byte_size
    return byte_size(conn, table, column, rowid)


def read_blob(conn, table, column, rowid, *, identity='ledger'):
    from backend.utils.sqlite_blob import blob_slice
    size = blob_size(conn, table, column, rowid)
    stack = _payload_lifetime.get()
    if stack is not None:
        stack.enter_context(_reserve(size))
        return _ReservedBlob(blob_slice(conn, table, column, rowid, length=size, expected_size=size))
    with _reserve(size):
        return blob_slice(conn, table, column, rowid, length=size, expected_size=size)


def one(conn, table, column, where, params, *, identity='ledger'):
    """Callers supply fixed SQL fragments; inspect size before selecting the blob."""
    row = conn.execute(f'SELECT storage_id AS rowid FROM {table} WHERE {where}', params).fetchone()
    if row is None:
        return None
    return decode(read_blob(conn, table, column, row[0], identity=identity), identity=identity)


def payload_page(conn, table, where, params, *, cursor=0, count=None, column='payload', order_by=None, ceiling=None):
    count = min(count or batch_size(), history_limit())
    condition, arguments, order = 'storage_id>%s', (*params, cursor), 'storage_id'
    if order_by == ('namespace', 'user_id'):
        order = 'namespace,user_id'
        condition, arguments = '1=1', tuple(params)
        if cursor:
            previous = conn.execute(f'SELECT namespace,user_id FROM {table} WHERE storage_id=%s', (cursor,)).fetchone()
            if previous is None:
                raise ValueError('Paper page cursor no longer exists')
            condition, arguments = '(namespace,user_id)>(%s,%s)', (*params, previous[0], previous[1])
    elif order_by is not None:
        raise ValueError('Unsupported paper page ordering')
    if ceiling is not None:
        condition += ' AND storage_id<=%s'
        arguments = (*arguments, ceiling)
    records = conn.execute(f'SELECT storage_id AS scan_id '
                           f'FROM {table} WHERE {where} AND {condition} ORDER BY {order} LIMIT %s',
                           (*arguments, count + 1)).fetchall()
    more = len(records) > count
    selected, skipped = [], []
    for record in records[:count]:
        row_id = record['scan_id']
        payload = read_blob(conn, table, column, row_id, identity=f'{table}:{row_id}')
        from backend.storage import table_columns
        fields = [row['name'] for row in table_columns(conn, table) if row['name'] not in {column, 'storage_id'}]
        row = conn.execute(f'SELECT {",".join(fields)},storage_id AS scan_id FROM {table} WHERE storage_id=%s', (row_id,)).fetchone()
        selected.append({**dict(row), column: decode(payload, identity=f'{table}:{row_id}')})
    next_cursor = records[min(len(records), count) - 1]['scan_id'] if records else cursor
    return selected, {'next_cursor': next_cursor, 'has_more': more, 'scan_complete': not more,
                      'skipped_records': skipped, 'status': 'partial' if more or skipped else 'complete'}


def receipt_revision(conn, namespace, user_id, operation_id, payload=None):
    """Preserve immutable observations; never replace the original receipt row."""
    import time
    from backend.storage import lock_scope
    lock_scope(conn, 'paper-receipt', namespace, user_id, operation_id)
    prior = conn.execute('SELECT storage_id AS rowid,revision FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s ORDER BY revision DESC LIMIT 1',
                         (namespace, user_id, operation_id)).fetchone()
    if payload is None:
        row = conn.execute('SELECT storage_id AS rowid FROM paper_batches WHERE namespace=%s AND user_id=%s AND operation_id=%s',
                               (namespace, user_id, operation_id)).fetchone()
        if row is None:
            return
        payload = read_blob(conn, 'paper_batches', 'payload', row[0])
    encoded = payload.decode('utf-8') if isinstance(payload, bytes) else json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False) if not isinstance(payload, str) else payload
    if prior is not None and read_blob(conn, 'paper_receipt_revisions', 'payload', prior['rowid']).decode('utf-8') == encoded:
        return
    conn.execute('INSERT INTO paper_receipt_revisions(namespace,user_id,operation_id,revision,observed_at,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                 (namespace, user_id, operation_id, prior['revision'] + 1 if prior else 0, time.time(), encoded))


def observe_receipt(conn, namespace, user_id, operation_id, original):
    """Update a separate immutable observation from original local order IDs."""
    latest = dict(original)
    active = False
    for field in ('order', 'orders', 'protection_orders'):
        candidates = [original[field]] if isinstance(original.get(field), dict) else original.get(field, [])
        current = []
        for order in candidates:
            if not isinstance(order, dict) or not order.get('order_id'):
                continue
            found = one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s',
                        (namespace, user_id, order['order_id']), identity='original receipt order')
            current.append(found or order)
            active |= ((found or order).get('state') == 'open' and order.get('exchange') == 'binance')
        if current:
            latest[field] = current[0] if field == 'order' else current
    order = latest.get('order') or {}
    if order:
        state = order.get('state')
        latest['order_status'] = 'waiting_trigger' if state == 'open' and order.get('order_type') in {'stop_loss', 'take_profit'} else state
        if state == 'filled':
            latest['status'] = 'completed'
            if order.get('fill'):
                latest['fill'] = order['fill']
                latest['filled_amount'] = order['fill']['amount']
        elif state in {'cancelled', 'rejected'}:
            latest['status'] = 'failed' if state == 'rejected' else 'completed'
    for field in ('orders', 'protection_orders') if not original.get('fill') and not order else ():
        filled = [order for order in latest.get(field, []) if order.get('state') == 'filled' and order.get('fill')]
        if filled:
            latest.update(status='completed', order_status='filled',
                          filled_amount=sum(order['fill']['amount'] for order in filled))
        elif latest.get(field) and all(order.get('state') in {'cancelled', 'rejected'} for order in latest[field]):
            rejected = any(order['state'] == 'rejected' for order in latest[field])
            latest.update(status='failed' if rejected else 'completed', order_status='rejected' if rejected else 'cancelled')
    if not conn.execute('SELECT 1 FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s LIMIT 1',
                        (namespace, user_id, operation_id)).fetchone():
        receipt_revision(conn, namespace, user_id, operation_id, original)
    receipt_revision(conn, namespace, user_id, operation_id, latest)
    conn.execute('INSERT INTO paper_receipt_watch(namespace,user_id,operation_id,active) VALUES(%s,%s,%s,%s) ON CONFLICT(namespace,user_id,operation_id) DO UPDATE SET active=excluded.active',
                 (namespace, user_id, operation_id, int(active)))
    return latest


@bounded(critical=True)
def recover_receipts():
    """Index old/new receipts once; subsequent ticks read only watched orders."""
    from . import store
    from backend.utils.runtime_capacity import CapacityBusy
    count = min(batch_size(), 8)
    with store.connection(recovery=True) as conn:
        saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='receipt-index'").fetchone()
        cursor = saved[0] if saved else 0
        new, progress = payload_page(conn, 'paper_operations', '1=1', (), cursor=cursor, count=count, column='result')
        for row in new:
            result = row['result']
            if any(result.get(name) for name in ('order', 'orders', 'protection_orders')):
                conn.execute('INSERT INTO paper_receipt_watch(namespace,user_id,operation_id,active) VALUES(%s,%s,%s,1) ON CONFLICT(namespace,user_id,operation_id) DO NOTHING',
                             (row['namespace'], row['user_id'], row['operation_id']))
        conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor',
                     ('receipt-index', progress['next_cursor'] if progress['has_more'] else 0, '{}'))
        saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='receipt-watch'").fetchone()
        cursor = saved[0] if saved else 0
        refs = conn.execute('SELECT storage_id AS rowid,namespace,user_id,operation_id FROM paper_receipt_watch WHERE active=1 AND storage_id>%s ORDER BY storage_id LIMIT %s',
                            (cursor, count + 1)).fetchall()
        refreshed, skipped, last = 0, list(progress['skipped_records']), cursor
        for ref in refs[:count]:
            try:
                original = one(conn, 'paper_operations', 'result', 'namespace=%s AND user_id=%s AND operation_id=%s',
                               (ref['namespace'], ref['user_id'], ref['operation_id']), identity='watched original paper receipt')
                if original is not None:
                    observe_receipt(conn, ref['namespace'], ref['user_id'], ref['operation_id'], original)
                    refreshed += 1
                last = ref['rowid']
            except PaperPayloadLimit:
                skipped.append({'operation_id': ref['operation_id'], 'reason_code': 'paper_payload_limit'})
                last = ref['rowid']
            except CapacityBusy:
                break
        more = len(refs) > count or last != (refs[-1]['rowid'] if refs else cursor)
        conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor',
                     ('receipt-watch', last if more else 0, '{}'))
        conn.commit()
    return {'refreshed': refreshed, 'skipped_records': skipped, 'scan_complete': not more and not progress['has_more']}


def _chunk(conn, table, column, where, parameters, *, token, offset, length, encoding='base64-utf8'):
    with _reserve((length * 4 + 3 if encoding == 'text' else length) * 2):
        return _read_chunk(conn, table, column, where, parameters, token=token, offset=offset, length=length, encoding=encoding)


def _read_chunk(conn, table, column, where, parameters, *, token, offset, length, encoding='base64-utf8'):
    if encoding not in {'base64-utf8', 'text'} or encoding == 'text' and length > 2048:
        raise ValueError('encoding must be base64-utf8, or text with length 1–2048 characters')
    row = conn.execute(f'SELECT storage_id AS rowid FROM {table} WHERE {where}', parameters).fetchone()
    if row is None:
        raise ValueError('Paper record not found in this account')
    import codecs
    from backend.utils.sqlite_blob import blob_slice
    size = blob_size(conn, table, column, row[0])
    data = blob_slice(conn, table, column, row[0], offset=min(offset, size),
                      length=min(size - min(offset, size), length * 4 + 3 if encoding == 'text' else length), expected_size=size)
    if encoding == 'text':
        try:
            value = codecs.getincrementaldecoder('utf-8')().decode(data, final=offset + len(data) >= size)[:length]
        except UnicodeError as exc:
            raise ValueError('Audit text offset must use the returned UTF-8 boundary cursor') from exc
        next_offset = offset + len(value.encode('utf-8'))
        return {'version': token, 'encoding': 'text', 'text': value, 'offset': offset, 'offset_unit': 'bytes',
                'page_character_limit': length, 'total_bytes': size,
                'next_offset': next_offset if next_offset < size else None,
                'has_more': next_offset < size, 'read_only': True}
    next_offset = offset + len(data)
    return {'version': token, 'encoding': 'base64-utf8', 'content': base64.b64encode(data).decode('ascii'),
            'offset': offset, 'total_bytes': size, 'next_offset': next_offset if next_offset < size else None,
            'has_more': next_offset < size, 'read_only': True}


def raw_audit_chunk(conn, namespace, user_id, kind, record_id=None, *, offset=0, length=16384, version=None, encoding='base64-utf8'):
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 65536:
        raise ValueError('Invalid bounded audit page')
    mapping = {'account': ('paper_accounts', 'user_id'), 'order': ('paper_orders', 'order_id'), 'event': ('paper_events', 'id')}
    if kind not in mapping:
        raise ValueError('Unsupported original ledger record')
    if conn.info.transaction_status == 0:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
    table, identifier = mapping[kind]
    record_id = str(user_id if kind == 'account' else record_id)
    where, params = f'namespace=%s AND user_id=%s AND {identifier}=%s', (namespace, user_id, record_id)
    row = conn.execute(f'SELECT storage_id AS rowid FROM {table} WHERE {where}', params).fetchone()
    if row is None:
        raise ValueError('Paper record not found in this account')
    revision = conn.execute('SELECT revision FROM paper_raw_versions WHERE namespace=%s AND user_id=%s AND kind=%s AND record_id=%s',
                            (namespace, user_id, kind, record_id)).fetchone()
    token = f'{kind}:{row[0]}:{revision[0] if revision else 0}'
    if version is not None and version != token:
        raise ValueError('audit_version_changed: restart this original ledger read')
    time_column = 'updated_at' if kind == 'order' else 'timestamp' if kind == 'event' else None
    observed = conn.execute(f'SELECT {time_column} FROM {table} WHERE storage_id=%s', (row[0],)).fetchone()[0] if time_column else None
    return {**_chunk(conn, table, 'payload', where, params, token=token, offset=offset, length=length, encoding=encoding),
            'record_type': kind, 'record_id': record_id, 'view': 'original_ledger', 'observed_at': observed,
            'evidence_time_status': 'recorded' if observed is not None else 'unavailable'}


def audit_chunk(conn, namespace, user_id, operation_id, *, offset=0, length=16384, version=None, view='original', encoding='base64-utf8'):
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 65536:
        raise ValueError('Invalid bounded audit page')
    if view not in {'original', 'latest'}:
        raise ValueError('view must be original or latest')
    if conn.info.transaction_status == 0:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ')
    if version is not None and version.startswith('receipt:'):
        revision = version.removeprefix('receipt:')
        if not revision.isdigit():
            raise ValueError('Invalid receipt revision')
        observed = conn.execute('SELECT observed_at FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s AND revision=%s',
                                (namespace, user_id, operation_id, int(revision))).fetchone()
        return {**_chunk(conn, 'paper_receipt_revisions', 'payload',
                        'namespace=%s AND user_id=%s AND operation_id=%s AND revision=%s',
                        (namespace, user_id, operation_id, int(revision)), token=version, offset=offset, length=length, encoding=encoding),
                'operation_id': operation_id, 'view': view, 'observed_at': observed[0] if observed else None,
                'evidence_time_status': 'recorded' if observed else 'unavailable'}
    # Child originals are immutable; parent observations have their own
    # immutable revision rows so later recovery cannot mix pages or erase ACKs.
    if version is None and (view == 'latest' or not conn.execute('SELECT 1 FROM paper_operations WHERE namespace=%s AND user_id=%s AND operation_id=%s',
                                                               (namespace, user_id, operation_id)).fetchone()):
        order = 'DESC' if view == 'latest' else 'ASC'
        revision = conn.execute(f'SELECT revision FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s ORDER BY revision {order} LIMIT 1',
                                (namespace, user_id, operation_id)).fetchone()
        if revision:
            return audit_chunk(conn, namespace, user_id, operation_id, offset=offset, length=length,
                               version='receipt:' + str(revision[0]), view=view, encoding=encoding)
    for table, column in (('paper_operations', 'result'), ('paper_batches', 'payload')):
        row = conn.execute(f'SELECT storage_id AS rowid,fingerprint '
                           f'FROM {table} WHERE namespace=%s AND user_id=%s AND operation_id=%s',
                           (namespace, user_id, operation_id)).fetchone()
        if row is None:
            continue
        # Original child receipts are immutable. A changing parent snapshot must
        # restart explicitly rather than mixing evidence versions across pages.
        updated = '' if table == 'paper_operations' else str(conn.execute(
            'SELECT updated_at FROM paper_batches WHERE storage_id=%s', (row['rowid'],)).fetchone()[0])
        current_version = f'{table}:{row["rowid"]}:{row["fingerprint"]}:{updated}'
        if version is not None and version != current_version:
            raise ValueError('audit_version_changed: restart the original record detail read')
        observed = float(updated) if updated else None
        if table == 'paper_operations':
            evidence = conn.execute('SELECT observed_at FROM paper_receipt_revisions WHERE namespace=%s AND user_id=%s AND operation_id=%s ORDER BY revision LIMIT 1',
                                    (namespace, user_id, operation_id)).fetchone()
            observed = evidence[0] if evidence else None
        return {**_chunk(conn, table, column, 'storage_id=%s', (row['rowid'],), token=current_version, offset=offset, length=length, encoding=encoding),
                'operation_id': operation_id, 'view': view, 'observed_at': observed,
                'evidence_time_status': 'recorded' if observed is not None else 'unavailable'}
    raise ValueError('Paper operation not found in this account')
