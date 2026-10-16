"""Bot competitors, capital-adjusted ranking and cursor-based notification feeds."""
from __future__ import annotations
from backend.storage import lock_scope

import json
import time
from decimal import Decimal

from . import paper, store


def ensure_bot_namespace(namespace):
    paper.identity(namespace, 'namespace')
    cfg = paper.settings()
    with store.connection() as conn:
        lock_scope(conn, 'paper-account', namespace, cfg['bot_user_id'])
        existing = conn.execute('SELECT 1 FROM paper_accounts WHERE namespace=%s AND user_id=%s', (namespace, cfg['bot_user_id'])).fetchone()
        if not existing:
            account = {'namespace': namespace, 'user_id': cfg['bot_user_id'], 'name': cfg['bot_name'],
                       'account_type': 'bot', 'cash': paper.money(cfg['initial_balance']), 'debt': 0,
                       'loan_limit': paper.money(cfg['loan_limit']), 'leverage': 1, 'markets': ['spot', 'swap'], 'enabled': True,
                       'spot': {}, 'positions': {}, 'realized_pnl': 0, 'fees': 0, 'created_at': time.time(),
                       'initial_capital': paper.money(cfg['initial_balance']), 'competition_capital': paper.money(cfg['initial_balance']),
                       'leaderboard_visible': True, 'notifications_enabled': True, 'reserved_cash': 0}
            paper.upgrade_account(conn, account)
            paper._save(conn, account)
            paper._event(conn, account, 'bot_created', {'account_type': 'bot'})
        conn.commit()


def bootstrap():
    from .paper_storage import payload_page
    with store.connection() as conn:
        saved = conn.execute("SELECT cursor FROM paper_scan_progress WHERE worker='bootstrap'").fetchone()
        keys, progress = payload_page(conn, 'mcp_records', "kind='key'", (), cursor=saved[0] if saved else 0)
        namespaces = {row['payload']['paper_namespace'] for row in keys if row['payload'].get('key_type') == 'paper' and not row['payload'].get('revoked')}
        conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor,payload=excluded.payload',
                     ('bootstrap', progress['next_cursor'] if progress['has_more'] else 0, '{}'))
        conn.commit()
    for namespace in namespaces:
        ensure_bot_namespace(namespace)
    return progress


def bot_account(caller):
    return paper.get_account(caller, paper.settings()['bot_user_id'])


def ensure_bot(caller, operation_id):
    cfg = paper.settings()
    return paper.create_account(caller, cfg['bot_user_id'], operation_id, cfg['bot_name'])


def leaderboard(caller, limit=20, metric='return_pct', user_id=None, *, administrator=False):
    import uuid
    from .paper_storage import payload_page, batch_size, PaperPayloadLimit
    namespace = paper.identity(caller['paper_namespace'], 'namespace') if administrator else paper.require(caller)
    if metric not in {'return_pct', 'reset_count'} or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('metric must be return_pct or reset_count; limit must be 1–200')
    if user_id is not None:
        paper.identity(user_id)
    worker = 'ranking:' + namespace + ':' + metric
    with store.connection() as conn:
        lock_scope(conn, 'paper-ranking', namespace)
        saved = conn.execute('SELECT cursor,payload FROM paper_scan_progress WHERE worker=%s', (worker,)).fetchone()
        state = json.loads(saved['payload']) if saved else {'generation': uuid.uuid4().hex, 'started_at': time.time(), 'skipped': 0}
        generation = state['generation']
        records, progress = payload_page(conn, 'paper_accounts', 'namespace=%s', (namespace,), cursor=saved['cursor'] if saved else 0, count=batch_size())
        state['skipped'] += len(progress['skipped_records'])
        for record in records:
            try:
                account = paper.upgrade_account(conn, record['payload'])
                if not account.get('leaderboard_visible', True):
                    continue
                view = paper.snapshot(account, conn=conn)
            except PaperPayloadLimit:
                state['skipped'] += 1
                continue
            capital = Decimal(str(account['competition_capital'])) if account['competition_capital'] is not None else None
            pct = paper.money(Decimal(str(view['performance_pnl'])) / capital * 100) if capital is not None and capital > 0 and view['performance_pnl'] is not None else None
            item = {'participant_id': store.digest(namespace + ':' + account['user_id'])[:16], 'name': account['name'],
                    'account_type': account['account_type'], 'reset_count': account['reset_count'], 'pnl': view['performance_pnl'], 'return_pct': pct,
                    'valuation_status': view['valuation_status'], 'oldest_mark_at': view['oldest_mark_at']}
            conn.execute('INSERT INTO paper_ranking_projection(namespace,generation,user_id,eligible,pnl,return_pct,reset_count,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(namespace,generation,user_id) DO UPDATE SET eligible=excluded.eligible,pnl=excluded.pnl,return_pct=excluded.return_pct,reset_count=excluded.reset_count,payload=excluded.payload',
                         (namespace, generation, account['user_id'], int(metric == 'reset_count' or view['performance_pnl'] is not None), view['performance_pnl'], pct, account['reset_count'], json.dumps(item, allow_nan=False)))
        if progress['has_more']:
            conn.execute('INSERT INTO paper_scan_progress(worker,cursor,payload) VALUES(%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor,payload=excluded.payload',
                         (worker, progress['next_cursor'], json.dumps(state)))
            conn.commit()
            return {'items': [], 'self': None, 'bot': None, 'total': None, 'unranked_count': None, 'incomplete_count': state['skipped'],
                    'status': 'partial', 'scan_complete': False, 'next_cursor': progress['next_cursor'], 'metric': metric,
                    'namespace': namespace, 'execution_mode': 'paper', 'observed_at': None, 'scan_started_at': state['started_at']}
        field = metric
        where = 'namespace=%s AND generation=%s AND eligible=1 AND ' + field + ' IS NOT NULL'
        total = conn.execute('SELECT COUNT(*) FROM paper_ranking_projection WHERE ' + where, (namespace, generation)).fetchone()[0]
        all_count = conn.execute('SELECT COUNT(*) FROM paper_ranking_projection WHERE namespace=%s AND generation=%s', (namespace, generation)).fetchone()[0]
        def render(row, rank=None):
            data = json.loads(row['payload'])
            if rank is None:
                rank = conn.execute('SELECT COUNT(*)+1 FROM paper_ranking_projection WHERE ' + where +
                                    ' AND (' + field + '>%s OR (' + field + "=%s AND (payload::jsonb->>'participant_id')<%s))",
                                    (namespace, generation, row[field], row[field], data['participant_id'])).fetchone()[0]
            data.update(rank=rank, is_self=row['user_id'] == user_id,
                        user_id=row['user_id'] if row['user_id'] == user_id or data['account_type'] == 'bot' else None)
            return data
        ranked = 'SELECT * FROM paper_ranking_projection WHERE ' + where
        ordering = ' ORDER BY ' + field + " DESC,(payload::jsonb->>'participant_id')"
        items = [render(row, index + 1) for index, row in enumerate(conn.execute(ranked + ordering + ' LIMIT %s', (namespace, generation, limit)))] if not state['skipped'] else []
        personal = conn.execute(ranked + ' AND user_id=%s', (namespace, generation, user_id)).fetchone() if user_id and not state['skipped'] else None
        bot = conn.execute(ranked + " AND (payload::jsonb->>'account_type')='bot'" + ordering + ' LIMIT 1', (namespace, generation)).fetchone() if not state['skipped'] else None
        personal = render(personal) if personal else None
        bot = render(bot) if bot else None
        incomplete = conn.execute('SELECT COUNT(*) FROM paper_ranking_projection WHERE namespace=%s AND generation=%s AND eligible=0', (namespace, generation)).fetchone()[0]
        conn.execute('DELETE FROM paper_scan_progress WHERE worker=%s', (worker,))
        conn.execute("DELETE FROM paper_ranking_projection WHERE namespace=%s AND generation<>%s AND generation NOT IN (SELECT payload::jsonb->>'generation' FROM paper_scan_progress WHERE worker LIKE 'ranking:%%')", (namespace, generation))
        conn.commit()
    return {'items': items, 'total': total if not state['skipped'] else None, 'metric': metric, 'namespace': namespace,
            'self': personal, 'bot': bot,
            'unranked_count': all_count - total + state['skipped'], 'incomplete_count': incomplete + state['skipped'],
            'status': 'partial' if state['skipped'] else 'complete', 'scan_complete': True, 'next_cursor': None,
            'execution_mode': 'paper', 'observed_at': time.time() if not state['skipped'] else None,
            'scan_started_at': state['started_at'],
            'scoring': 'Complete Binance valuation minus initial and administrator-adjusted capital; loans do not increase performance.'}


def preferences(caller, user_id, operation_id, leaderboard_visible=None, notifications_enabled=None, name=None):
    updates = {k: v for k, v in {'leaderboard_visible': leaderboard_visible, 'notifications_enabled': notifications_enabled, 'name': name}.items() if v is not None}
    for field in ('leaderboard_visible', 'notifications_enabled'):
        if field in updates and type(updates[field]) is not bool:
            raise ValueError('Preferences must be booleans')
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 100):
        raise ValueError('Name must be 1–100 characters')
    def change(account, conn):
        account.update(updates)
        return {'preferences': updates}
    return paper._write(caller, user_id, operation_id, {'action': 'preferences', **updates}, change)


def scan(caller, after_id=0, limit=100, user_id=None, notifications_only=True):
    from .paper_storage import payload_page, one, PaperPayloadLimit
    from .paper_policy import event_visible
    namespace = paper.require(caller)
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('after_id must be nonnegative and limit must be 1–200')
    where, params = 'namespace=%s', [namespace]
    if user_id is not None:
        where += ' AND user_id=%s'
        params.append(paper.identity(user_id))
    with store.connection() as conn:
        events, progress = payload_page(conn, 'paper_events', where, params, cursor=after_id, count=limit)
        items, omitted = [], len(progress['skipped_records'])
        for event in events:
            try:
                account = one(conn, 'paper_accounts', 'payload', 'namespace=%s AND user_id=%s', (namespace, event['user_id']), identity='notification preferences')
                if not account or not event_visible(event, conn=conn, account=account):
                    omitted += 1
                    continue
                if notifications_only and (event['kind'] not in {'order_filled', 'liquidation', 'order_rejected'} or not account.get('notifications_enabled', True)):
                    continue
                items.append({**{k: v for k, v in event.items() if k != 'scan_id'}, 'account_name': account['name'],
                              'account_type': account.get('account_type', 'bot' if account['user_id'].startswith('bot:') else 'user'),
                              'recipient_user_id': event['user_id'], 'delivery_key': f'paper:{namespace}:{event["id"]}',
                              'execution_mode': 'paper', 'display_timezone': 'Asia/Shanghai'})
            except PaperPayloadLimit:
                omitted += 1
        conn.commit()
    return {'events': items, **progress, 'filtered_count': omitted, 'namespace': namespace, 'delivery_semantics': 'at_least_once',
            'note': 'Deliver or durably enqueue returned events before persisting next_cursor; empty filtered pages still advance. Deduplicate by delivery_key. This endpoint does not send QQ messages.'}


def admin_data(namespace=None, user_id=None, limit=100, *, cursor=0, namespace_cursor=''):
    from .paper_storage import payload_page, batch_size
    from .paper_policy import event_visible
    clauses, params = [], []
    for name, value in [('namespace', namespace), ('user_id', user_id)]:
        if value:
            clauses.append(name + '=%s')
            params.append(paper.identity(value, name))
    where = ' AND '.join(clauses) or '1=1'
    with store.connection() as conn:
        records, progress = payload_page(conn, 'paper_events', where, params, cursor=cursor, count=limit)
        events = [record for record in records if event_visible(record, conn=conn, account={'namespace': record['namespace'], 'user_id': record['user_id']})]
        spaces = [namespace] if namespace else [row[0] for row in conn.execute('SELECT DISTINCT namespace FROM paper_accounts WHERE namespace>%s ORDER BY namespace LIMIT %s',
                                                                             (namespace_cursor, batch_size() + 1))]
        conn.commit()
    rankings, reset_rankings, ranking_states = [], [], []
    for space in spaces[:batch_size()]:
        value = leaderboard({'paper_namespace': space}, min(limit, 200), administrator=True)
        rankings.extend(value['items'])
        resets = leaderboard({'paper_namespace': space}, min(limit, 200), metric='reset_count', administrator=True)
        reset_rankings.extend(resets['items'])
        ranking_states.append({'namespace': space, 'metric': 'reset_count', 'status': resets['status'], 'scan_complete': resets['scan_complete'], 'next_cursor': resets['next_cursor'], 'incomplete_count': resets['incomplete_count'], 'unranked_count': resets['unranked_count']})
        ranking_states.append({'namespace': space, 'metric': 'return_pct', 'status': value['status'], 'scan_complete': value['scan_complete'], 'next_cursor': value['next_cursor'],
                               'incomplete_count': value['incomplete_count'], 'unranked_count': value['unranked_count']})
    partial = len(spaces) > batch_size() or progress['status'] == 'partial' or any(row['status'] != 'complete' for row in ranking_states)
    return {'leaderboard': rankings[:limit], 'reset_leaderboard': reset_rankings[:limit], 'rankings': ranking_states, 'events': events, **progress,
            'status': 'partial' if partial else 'complete', 'filtered_count': len(records) - len(events),
            'namespaces': {'has_more': len(spaces) > batch_size(), 'scan_complete': len(spaces) <= batch_size(),
                           'status': 'partial' if len(spaces) > batch_size() else 'complete',
                           'next_cursor': spaces[batch_size() - 1] if len(spaces) > batch_size() else None}}


from .paper_storage import bounded as _bounded
for _name in ('ensure_bot_namespace', 'bootstrap', 'leaderboard', 'scan', 'admin_data'):
    globals()[_name] = _bounded(globals()[_name])
