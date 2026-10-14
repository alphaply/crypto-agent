"""Bot competitors, capital-adjusted ranking and cursor-based notification feeds."""
from __future__ import annotations

import json
import time
from decimal import Decimal

from . import paper, store


def ensure_bot_namespace(namespace):
    paper.identity(namespace, 'namespace')
    cfg = paper.settings()
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT 1 FROM paper_accounts WHERE namespace=? AND user_id=?', (namespace, cfg['bot_user_id'])).fetchone()
        if not existing:
            account = {'namespace': namespace, 'user_id': cfg['bot_user_id'], 'name': cfg['bot_name'],
                       'account_type': 'bot', 'cash': paper.money(cfg['initial_balance']), 'debt': 0,
                       'loan_limit': paper.money(cfg['loan_limit']), 'leverage': 1, 'markets': ['spot', 'swap'], 'enabled': True,
                       'spot': {}, 'positions': {}, 'realized_pnl': 0, 'fees': 0, 'created_at': time.time(),
                       'initial_capital': paper.money(cfg['initial_balance']), 'competition_capital': paper.money(cfg['initial_balance']),
                       'leaderboard_visible': True, 'notifications_enabled': True, 'reserved_cash': 0}
            paper._save(conn, account)
            paper._event(conn, account, 'bot_created', {'account_type': 'bot'})
        conn.commit()


def bootstrap():
    with store.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        accounts = [json.loads(row[0]) for row in conn.execute('SELECT payload FROM paper_accounts')]
        for account in accounts:
            paper.upgrade_account(conn, account)
            paper._save(conn, account)
        namespaces = {json.loads(r[0])['paper_namespace'] for r in conn.execute("SELECT payload FROM mcp_records WHERE kind='key'")
                      if json.loads(r[0]).get('key_type') == 'paper'}
        conn.commit()
    for namespace in namespaces:
        ensure_bot_namespace(namespace)


def bot_account(caller):
    return paper.get_account(caller, paper.settings()['bot_user_id'])


def ensure_bot(caller, operation_id):
    cfg = paper.settings()
    return paper.create_account(caller, cfg['bot_user_id'], operation_id, cfg['bot_name'])


def leaderboard(caller, limit=20, metric='return_pct', user_id=None, *, administrator=False):
    namespace = paper.identity(caller['paper_namespace'], 'namespace') if administrator else paper.require(caller)
    if metric not in {'return_pct', 'pnl'} or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('metric must be return_pct or pnl; limit must be 1–200')
    if user_id is not None:
        paper.identity(user_id)
    with store.connection() as conn:
        accounts = [paper.upgrade_account(conn, json.loads(r[0])) for r in conn.execute('SELECT payload FROM paper_accounts WHERE namespace=?', (namespace,))]
    items = []
    for account in accounts:
        if not account.get('leaderboard_visible', True):
            continue
        view = paper.snapshot(account)
        capital = Decimal(str(account['competition_capital']))
        pct = paper.money(Decimal(str(view['performance_pnl'])) / capital * 100) if capital > 0 else None
        items.append({'participant_id': store.digest(namespace + ':' + account['user_id'])[:16], 'name': account['name'],
                      'account_type': account['account_type'], 'pnl': view['performance_pnl'], 'return_pct': pct,
                      'valuation_status': view['valuation_status'], 'oldest_mark_at': view['oldest_mark_at'],
                      'is_self': account['user_id'] == user_id, 'user_id': account['user_id'] if account['user_id'] == user_id or account['account_type'] == 'bot' else None})
    eligible = [r for r in items if metric == 'pnl' or r['return_pct'] is not None]
    eligible.sort(key=lambda r: (-(r['return_pct'] if metric == 'return_pct' else r['pnl']), r['participant_id']))
    for rank, row in enumerate(eligible, 1):
        row['rank'] = rank
    return {'items': eligible[:limit], 'total': len(eligible), 'metric': metric, 'namespace': namespace,
            'self': next((r for r in eligible if r['is_self']), None), 'bot': next((r for r in eligible if r['account_type'] == 'bot'), None),
            'unranked_count': len(items) - len(eligible), 'execution_mode': 'paper', 'observed_at': time.time(),
            'scoring': 'Net equity minus initial and administrator-adjusted capital; loans do not increase performance.'}


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
    namespace = paper.require(caller)
    if type(after_id) is not int or after_id < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('after_id must be nonnegative and limit must be 1–200')
    query, params = 'SELECT * FROM paper_events WHERE namespace=? AND id>?', [namespace, after_id]
    if user_id is not None:
        query += ' AND user_id=?'
        params.append(paper.identity(user_id))
    with store.connection() as conn:
        events = conn.execute(query + ' ORDER BY id LIMIT ?', [*params, limit + 1]).fetchall()
        has_more = len(events) > limit
        events = events[:limit]
        items = []
        for event in events:
            account = paper._get(conn, namespace, event['user_id'])
            if notifications_only and (event['kind'] not in {'order_filled', 'liquidation', 'order_rejected'} or not account.get('notifications_enabled', True)):
                continue
            payload = json.loads(event['payload'])
            items.append({**dict(event), 'payload': payload, 'account_name': account['name'], 'account_type': account['account_type'],
                          'recipient_user_id': event['user_id'], 'delivery_key': f'paper:{namespace}:{event["id"]}',
                          'execution_mode': 'paper', 'display_timezone': 'Asia/Shanghai'})
    return {'events': items, 'next_cursor': events[-1]['id'] if events else after_id, 'has_more': has_more,
            'namespace': namespace, 'delivery_semantics': 'at_least_once',
            'note': 'Deliver or durably enqueue returned events before persisting next_cursor; deduplicate by delivery_key. This endpoint does not send QQ messages.'}


def admin_data(namespace=None, user_id=None, limit=100):
    with store.connection() as conn:
        spaces = [namespace] if namespace else [r[0] for r in conn.execute('SELECT DISTINCT namespace FROM paper_accounts')]
    rankings, events = [], []
    for space in spaces:
        caller = {'key_type': 'paper', 'paper_namespace': space, 'scopes': ['read'], 'id': 'admin'}
        # Administrator reads stay usable while paper matching is disabled.
        with store.connection() as conn:
            query, params = 'SELECT * FROM paper_events WHERE namespace=?', [space]
            if user_id:
                query += ' AND user_id=?'
                params.append(user_id)
            events.extend({**dict(r), 'payload': json.loads(r['payload'])} for r in conn.execute(query + ' ORDER BY id DESC LIMIT ?', [*params, limit]))
        rankings.extend(leaderboard(caller, min(limit, 200), administrator=True)['items'])
    return {'leaderboard': rankings, 'events': sorted(events, key=lambda r: r['id'], reverse=True)[:limit]}
