"""Trusted Binance policy and evidence projections, separate from raw finances."""
from __future__ import annotations

import hashlib
import json
import uuid

from . import paper_storage

VERSION = 1
NON_TRADING_EVENTS = {'create', 'bot_created', 'borrow', 'repay', 'leverage', 'preferences', 'admin_adjustment'}


def venue(value=None):
    if value is None:
        return 'binance'
    if not isinstance(value, str) or value.strip().lower() not in {'binance', 'bn'}:
        raise ValueError('paper_venue_out_of_scope: Paper only supports Binance')
    return 'binance'


def stamp(record, *, operation_id=None, resource_id=None):
    venue(record.get('exchange'))
    record.update(exchange='binance', origin_version=VERSION, origin_exchange='binance',
                  resource_id=resource_id or record.get('resource_id') or uuid.uuid4().hex)
    if operation_id:
        record.setdefault('origin_operation_id', operation_id)
    return record


def _receipt_sources(conn, namespace, user_id, resource, kind, key):
    """Only immutable original snapshots tied to this exact resource/cycle."""
    with paper_storage.transient_payloads():
        return _scan_receipt_sources(conn, namespace, user_id, resource, kind, key)


def _scan_receipt_sources(conn, namespace, user_id, resource, kind, key):
    fingerprint = resource_fingerprint(resource)
    saved = conn.execute('SELECT fingerprint,payload FROM paper_origin_scans WHERE namespace=%s AND user_id=%s AND kind=%s AND resource_key=%s',
                         (namespace, user_id, kind, key)).fetchone()
    revision_row = conn.execute('SELECT revision FROM paper_source_revisions WHERE namespace=%s AND user_id=%s', (namespace, user_id)).fetchone()
    revision = revision_row[0] if revision_row else 0
    previous = json.loads(saved['payload']) if saved and saved['fingerprint'] == fingerprint else None
    state = previous if previous and previous.get('source_revision') == revision else {
        'source_revision': revision,
        'cursor': 0, 'horizon': previous['horizon'] if previous else conn.execute('SELECT COALESCE(MAX(storage_id),0) FROM paper_operations WHERE namespace=%s AND user_id=%s',
                                            (namespace, user_id)).fetchone()[0],
        'sources': [], 'complete': False, 'origin_seen': False, 'evidence': []}
    if state['complete']:
        return set(state['sources']), True
    sources, cursor, completed, origin_seen = set(state['sources']), state['cursor'], False, state['origin_seen']
    for _ in range(1):
        rows, page = paper_storage.payload_page(conn, 'paper_operations', 'namespace=%s AND user_id=%s',
                                                (namespace, user_id), cursor=cursor, column='result',
                                                ceiling=state['horizon'])
        for row in rows:
            result = row['result']
            targets = []
            if kind == 'orders':
                targets = [result.get('order') or {}, *result.get('orders', []), *result.get('protection_orders', [])]
                targets = [item for item in targets if item.get('order_id') == resource.get('order_id')]
            else:
                collection = (result.get('account') or {}).get(kind, [])
                collection = list(collection.values()) if isinstance(collection, dict) else collection
                if resource.get('cycle_id'):
                    targets = [item for item in collection if item.get('cycle_id') == resource['cycle_id']
                               and item.get('symbol') == resource.get('symbol', key)
                               and (kind != 'positions' or item.get('side') == resource.get('side'))]
                fill = result.get('fill') or {}
                if targets:
                    if origin_seen:
                        targets = []
                    else:
                        origin_seen = True
                        expected_action = 'buy' if kind == 'spot' else 'open_' + str(resource.get('side', '')).lower()
                        related = (fill.get('symbol') == resource.get('symbol', key) and
                                   fill.get('action') == expected_action and
                                   (not fill.get('cycle_id') or fill['cycle_id'] == resource.get('cycle_id')) and
                                   (not resource.get('resource_id') or not fill.get('resource_id') or fill['resource_id'] == resource['resource_id']))
                        if not related:
                            sources.add('incomplete_evidence')
                            targets = []
                        elif any(item.get('exchange') != fill.get('exchange') for item in targets):
                            sources.add('conflicting_origin')
            for item in targets:
                if item.get('exchange'):
                    sources.add(item['exchange'] if item['exchange'] in {'binance', 'okx', 'bybit', 'bitget'} else 'unverified_venue')
                    if len(state['evidence']) < 4:
                        state['evidence'].append({'operation_id': row['operation_id'], 'fingerprint': row['fingerprint']})
        cursor = page['next_cursor']
        if page['skipped_records']:
            sources.add('incomplete_evidence')
        if not page['has_more']:
            completed = True
            break
    latest = conn.execute('SELECT revision FROM paper_source_revisions WHERE namespace=%s AND user_id=%s', (namespace,user_id)).fetchone()
    completed = completed and (latest[0] if latest else 0) == revision
    state.update(cursor=cursor, sources=sorted(sources), complete=completed, origin_seen=origin_seen)
    conn.execute('INSERT INTO paper_origin_scans(namespace,user_id,kind,resource_key,fingerprint,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,user_id,kind,resource_key) DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload',
                 (namespace, user_id, kind, key, fingerprint, json.dumps(state, separators=(',', ':'))))
    return sources, completed


def classify(resource, *, account=None, kind='orders', key='', conn=None):
    key = key or resource.get('order_id', '')
    if resource.get('origin_version') == VERSION and resource.get('origin_exchange') == resource.get('exchange') == 'binance':
        return {'classification': 'binance', 'reason_code': 'trusted_financial_origin', 'exchange': 'binance'}
    cached = (account or {}).get('_venue_origins', {}).get(kind + ':' + key)
    if cached:
        return cached
    if resource.get('exchange') and resource['exchange'] not in {'binance', 'bn'}:
        return {'classification': 'excluded_venue', 'reason_code': 'paper_venue_out_of_scope', 'exchange': resource['exchange']}
    if conn is not None and account is not None:
        sources, complete = _receipt_sources(conn, account['namespace'], account['user_id'], resource, kind, key)
        if complete and sources == {'binance'}:
            return {'classification': 'binance', 'reason_code': 'original_receipt_proves_origin', 'exchange': 'binance'}
        if complete and len(sources) == 1 and sources <= {'okx', 'bybit', 'bitget'}:
            return {'classification': 'excluded_venue', 'reason_code': 'paper_venue_out_of_scope', 'exchange': next(iter(sources))}
    return {'classification': 'unresolved_origin', 'reason_code': 'paper_origin_unverified', 'exchange': None}


def visible(resource, **context):
    return classify(resource, **context)['classification'] == 'binance'


def require_target(resource, **context):
    fact = classify(resource, **context)
    if fact['classification'] != 'binance':
        raise ValueError(fact['reason_code'] + ': historical target is excluded; raw state and financial commitments retained')
    return fact


def resource_fingerprint(resource):
    return hashlib.sha256(json.dumps({name: resource.get(name) for name in
        ('resource_id', 'cycle_id', 'symbol', 'side', 'exchange', 'origin_version', 'origin_operation_id')},
        sort_keys=True).encode()).hexdigest()


def persist_origin(conn, account, kind, key, resource, fact):
    fingerprint = resource_fingerprint(resource)
    conn.execute('INSERT INTO paper_resource_origins(namespace,user_id,kind,resource_key,fingerprint,classification,reason_code,policy_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,user_id,kind,resource_key) DO UPDATE SET fingerprint=excluded.fingerprint,classification=excluded.classification,reason_code=excluded.reason_code,policy_version=excluded.policy_version',
                 (account['namespace'], account['user_id'], kind, key, fingerprint, fact['classification'], fact['reason_code'], VERSION))


def account_facts(account, conn=None):
    facts = {}
    for kind in ('spot', 'positions'):
        for key, item in account[kind].items():
            facts[(kind, key)] = classify(item, account=account, kind=kind, key=key, conn=conn)
    return facts


def event_visible(event, *, conn=None, account=None):
    if event['kind'] in NON_TRADING_EVENTS:
        return True
    payload = event['payload']
    fill = payload.get('fill') or {}
    if fill:
        return fill.get('exchange') == 'binance'
    if payload.get('origin_version') == VERSION:
        return payload.get('exchange') == 'binance'
    embedded = [payload['order']] if payload.get('order') else payload.get('orders', [])
    if embedded:
        return all(visible(order, account=account, conn=conn) for order in embedded)
    if payload.get('order_id') and conn is not None and account is not None:
        order = paper_storage.one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s',
                                  (account['namespace'], account['user_id'], payload['order_id']), identity='event order')
        return bool(order and visible(order, account=account, conn=conn))
    return False


def performance_complete(conn, account):
    """Closed foreign positions also prevent a Binance-only performance score."""
    with paper_storage.transient_payloads():
        return _performance_scan(conn, account)


def _performance_scan(conn, account):
    namespace, user_id = account['namespace'], account['user_id']
    worker = 'performance:' + hashlib.sha256((namespace + '\0' + user_id).encode()).hexdigest()
    saved = conn.execute('SELECT cursor FROM paper_scan_progress WHERE worker=%s', (worker,)).fetchone()
    cursor = saved[0] if saved else 0
    events, progress = paper_storage.payload_page(conn, 'paper_events', 'namespace=%s AND user_id=%s',
        (namespace, user_id), cursor=cursor)

    def remember(event):
        if event_visible(event, conn=conn, account=account):
            state = 'binance'
        else:
            fill = event['payload'].get('fill') or {}
            state = 'excluded' if fill.get('exchange') in {'okx', 'bybit', 'bitget'} else 'unresolved'
        conn.execute('INSERT INTO paper_performance_events(namespace,user_id,event_id,state) VALUES(%s,%s,%s,%s) ON CONFLICT(namespace,user_id,event_id) DO UPDATE SET state=excluded.state',
                     (namespace, user_id, event['id'], state))

    for event in events:
        remember(event)
    for skipped in progress['skipped_records']:
        conn.execute('INSERT INTO paper_performance_events(namespace,user_id,event_id,state) VALUES(%s,%s,%s,%s) ON CONFLICT(namespace,user_id,event_id) DO NOTHING',
                     (namespace, user_id, skipped['scan_id'], 'unresolved'))
    conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor',
                 (worker, progress['next_cursor'], '{}'))
    # Retry only a bounded page of unresolved source proofs. A newly completed
    # origin scan can restore eligibility; closed foreign fills never can.
    retry_worker = worker + ':unresolved'
    saved = conn.execute('SELECT cursor FROM paper_scan_progress WHERE worker=%s', (retry_worker,)).fetchone()
    retry_cursor = saved[0] if saved else 0
    refs = conn.execute("SELECT event_id FROM paper_performance_events WHERE namespace=%s AND user_id=%s AND state='unresolved' AND event_id>%s ORDER BY event_id LIMIT %s",
                        (namespace, user_id, retry_cursor, paper_storage.batch_size())).fetchall()
    for ref in refs:
        try:
            payload = paper_storage.one(conn, 'paper_events', 'payload', 'namespace=%s AND user_id=%s AND id=%s',
                                        (namespace, user_id, ref['event_id']), identity='performance event')
        except paper_storage.PaperPayloadLimit:
            continue
        if payload is None:
            continue
        row = conn.execute('SELECT kind FROM paper_events WHERE id=%s', (ref['event_id'],)).fetchone()
        remember({'id': ref['event_id'], 'kind': row[0], 'payload': payload})
    conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor',
                 (retry_worker, refs[-1]['event_id'] if len(refs) == paper_storage.batch_size() else 0, '{}'))
    unresolved = conn.execute("SELECT 1 FROM paper_performance_events WHERE namespace=%s AND user_id=%s AND state!='binance' LIMIT 1",
                              (namespace, user_id)).fetchone()
    if progress['has_more']:
        return False, 'paper_performance_scan_incomplete'
    return (False, 'paper_historical_performance_unverified') if unresolved else (True, None)


def preflight_actions(conn, account, actions):
    """The whole static venue/known-target boundary precedes the first step."""
    from .paper_market import canonical
    planned_markets = set()
    for action in actions:
        values = action.model_dump(exclude_none=True) if hasattr(action, 'model_dump') else dict(action)
        venue(values.get('exchange'))
        kind, action_name = values.get('market_type', 'swap'), values['action']
        symbol = canonical(values['symbol'], kind) if values.get('symbol') else None
        side = values.get('side', 'LONG')
        if action_name in {'edit', 'cancel'}:
            target = paper_storage.one(conn, 'paper_orders', 'payload', 'namespace=%s AND user_id=%s AND order_id=%s',
                                       (account['namespace'], account['user_id'], values['order_id']), identity='batch original target')
            if target is None:
                raise ValueError('Order not found in this account; no financial step started')
            require_target(target, account=account, conn=conn)
        elif action_name in {'close', 'protect'}:
            collection = account['spot' if kind == 'spot' else 'positions']
            targets = [(key, item) for key, item in collection.items() if item.get('symbol', key) == symbol
                       and (kind == 'spot' or item.get('side') == side)]
            if targets and not any(visible(item, account=account, kind='spot' if kind == 'spot' else 'positions', key=key, conn=conn)
                                   for key, item in targets) and (kind, symbol, side) not in planned_markets:
                raise ValueError('paper_origin_unverified: batch target is excluded; no financial step started')
        if action_name == 'open' and values.get('order_type', 'limit') == 'market':
            planned_markets.add((kind, symbol, side))
