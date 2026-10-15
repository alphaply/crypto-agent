"""Bounded original-identity observations. Never dispatch financial callbacks."""
from __future__ import annotations

import json
import time
from contextlib import ExitStack

from backend.utils.runtime_capacity import capacity_scope, positive_setting, CapacityBusy, capacity_limits_enabled


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS background_receipt_retries (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, attempts INTEGER NOT NULL,
        next_attempt REAL NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(config_id,operation_id))''')


def _pending(value):
    if not isinstance(value, dict):
        return False
    if value.get('status') in {'running', 'unknown', 'pending', 'submitted'}:
        return True
    if isinstance(value.get('cleanup'), dict) and value['cleanup'].get('status') in {'pending', 'unknown'}:
        return True
    return any(_pending(row.get('result', row)) for row in value.get('results', []) if isinstance(row, dict))


def reconcile(*, limit=32, after_cursor=None):
    from backend.database import get_db_conn
    from backend.config import config
    from backend.utils.trade_operations import _decode_receipt, tool_result_status
    from backend.utils.trade_evidence import append_receipt
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('Background receipt batch must be 1..128')
    cursor = int(after_cursor or 0)
    if cursor < 0:
        raise ValueError('Invalid background receipt cursor')
    cap = positive_setting('RUNTIME_FINANCIAL_PAYLOAD_BYTES', 1048576)
    deadline = time.monotonic() + positive_setting('RUNTIME_RECOVERY_SECONDS', 10.0)
    with capacity_scope('recovery', critical=True, timeout=0):
        with get_db_conn() as conn:
            initialize(conn)
            rows = conn.execute('''SELECT r.rowid AS scan_id,q.rowid AS request_rowid,r.config_id,r.operation_id,r.symbol,
                COALESCE(b.attempts,0) AS attempts,COALESCE(b.next_attempt,0) AS next_attempt
                FROM trade_action_runs r LEFT JOIN trade_operation_requests q
                ON q.config_id=r.config_id AND q.operation_id=r.operation_id
                LEFT JOIN background_receipt_retries b ON b.config_id=r.config_id AND b.operation_id=r.operation_id
                WHERE r.rowid>? ORDER BY r.rowid LIMIT ?''', (cursor, limit)).fetchall()
            conn.commit()
        processed, skipped, next_cursor = 0, [], cursor
        for metadata in rows:
            if time.monotonic() >= deadline:
                break
            next_cursor = metadata['scan_id']
            row = dict(metadata)
            if row['next_attempt'] > time.time():
                continue
            with ExitStack() as payloads:
                try:
                    from backend.utils.sqlite_blob import bounded_text
                    with get_db_conn() as conn:
                        row['result'] = payloads.enter_context(bounded_text(conn, 'trade_action_runs', 'result', row['scan_id'], limit=cap))
                        row['request_json'] = payloads.enter_context(bounded_text(conn, 'trade_operation_requests', 'request_json', row['request_rowid'], limit=cap))
                except (CapacityBusy, TypeError):
                    skipped.append({'operation_id': row['operation_id'], 'reason': 'bounded_payload_unavailable'})
                    continue
                _observe(row, cap, deadline)
                processed += int(row.get('observed', False))
        complete = not rows or len(rows) < limit and next_cursor == rows[-1]['scan_id']
        outer = reconcile_mcp(limit=limit, cap=cap, deadline=deadline)
        return {'processed': processed, 'skipped': skipped, 'scan_complete': complete,
                'mcp': outer, 'next_cursor': 0 if complete else next_cursor}


def _observe(row, cap, deadline):
    from backend.database import get_db_conn
    from backend.config import config
    from backend.utils.trade_operations import _decode_receipt, tool_result_status
    from backend.utils.trade_evidence import append_receipt
    previous = _decode_receipt(row['result'])
    if not _pending(previous):
        return
    cfg = config.get_config_by_id(row['config_id'])
    if not cfg:
        # Deleted/retired principals retain evidence; no current authority
        # can be inferred from a formerly valid task identity.
        return
    outcome = 'observed'
    try:
        from backend.mcp.resilience import Budget, current_budget
        budget = Budget(deadline, 'background-receipt', 'read')
        token = current_budget.set(budget)
        try:
            request = json.loads(row['request_json'])
            if request.get('contract_version') == 3:
                from backend.agent.unified_trading import query_unified_operation
                latest = query_unified_operation(row['config_id'], row['symbol'], row['operation_id'], read_exchange=True)
            elif cfg.get('mode') == 'STRATEGY':
                from backend.utils.trade_operations import reconcile_pending_trade_operations
                reconcile_pending_trade_operations(row['config_id'], operation_id=row['operation_id'])
                with get_db_conn() as conn:
                    updated = conn.execute('SELECT rowid FROM trade_action_runs WHERE config_id=? AND operation_id=?',
                        (row['config_id'], row['operation_id'])).fetchone()
                    from backend.utils.sqlite_blob import bounded_text
                    if updated:
                        with bounded_text(conn, 'trade_action_runs', 'result', updated[0], limit=cap) as raw:
                            latest = _decode_receipt(raw)
                    else:
                        latest = previous
            else:
                from backend.agent.unified_dispatch import refresh_native_operation
                latest = refresh_native_operation(row['config_id'], row['symbol'], row['operation_id'], previous)
                # Legacy version and request identity must not be upgraded
                # by the v3 native observation adapter.
                for key in ('contract_version', 'receipt_version'):
                    if key in previous:
                        latest[key] = previous[key]
                    else:
                        latest.pop(key, None)
        finally:
            current_budget.reset(token)
        if isinstance(latest, dict) and latest != previous:
            encoded = json.dumps(latest, ensure_ascii=False, allow_nan=False, default=str)
            if capacity_limits_enabled() and len(encoded.encode('utf-8')) > cap:
                raise CapacityBusy('recovery', 'receipt_bytes')
            with get_db_conn() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT 1 FROM trade_action_runs WHERE config_id=? AND operation_id=? AND result=?',
                    (row['config_id'], row['operation_id'], row['result'])).fetchone()
                # A concurrent execution/recovery always wins over our old
                # observation. The next pass reloads that version.
                if current:
                    append_receipt(conn, row['config_id'], row['operation_id'], latest)
                    conn.execute('UPDATE trade_action_runs SET result=?,status=?,updated_at=? WHERE config_id=? AND operation_id=?',
                        (encoded, tool_result_status(latest), time.time(), row['config_id'], row['operation_id']))
                conn.commit()
        if isinstance(latest, dict) and latest.get('query', {}).get('status') == 'failed':
            outcome = 'read_failed'
        row['observed'] = True
    except Exception:
        outcome = 'read_failed'
    attempts = min(row['attempts'] + 1, 12)
    delay = min(300, 5 * 2 ** min(attempts - 1, 6)) if outcome == 'read_failed' else 5
    with get_db_conn() as conn:
        conn.execute('INSERT INTO background_receipt_retries VALUES(?,?,?,?,?) ON CONFLICT(config_id,operation_id) '
            'DO UPDATE SET attempts=excluded.attempts,next_attempt=excluded.next_attempt,result=excluded.result',
            (row['config_id'], row['operation_id'], attempts if outcome == 'read_failed' else 0, time.time() + delay, outcome))
        conn.commit()


def reconcile_mcp(*, limit, cap, deadline):
    from backend.mcp import store, service
    from backend.mcp.settings import get_runtime_profile
    from backend.mcp.resilience import Budget, current_budget
    from backend.mcp.canonical_tools import recover_unified
    with store.connection(recovery=True) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS background_receipt_progress (worker TEXT PRIMARY KEY,cursor INTEGER NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS background_mcp_retries (profile_id TEXT,operation_id TEXT,attempts INTEGER,next_attempt REAL,PRIMARY KEY(profile_id,operation_id))')
        saved = conn.execute("SELECT cursor FROM background_receipt_progress WHERE worker='mcp'").fetchone()
        cursor = saved[0] if saved else 0
        rows = conn.execute('SELECT rowid AS scan_id,profile_id,operation_id,principal,state,result IS NULL AS missing_result '
            'FROM mcp_operations WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor, limit)).fetchall()
        conn.commit()
    processed = 0
    for row in rows:
        if time.monotonic() >= deadline:
            break
        cursor = row['scan_id']
        with ExitStack() as payloads:
            from backend.utils.sqlite_blob import bounded_text
            with store.connection(recovery=True) as conn:
                retry = conn.execute('SELECT attempts,next_attempt FROM background_mcp_retries WHERE profile_id=? AND operation_id=?', (row['profile_id'], row['operation_id'])).fetchone()
                if retry and retry['next_attempt'] > time.time():
                    continue
                try:
                    raw = '{}' if row['missing_result'] else payloads.enter_context(bounded_text(conn, 'mcp_operations', 'result', row['scan_id'], limit=cap))
                except CapacityBusy:
                    continue
            old = json.loads(raw)
            if row['state'] not in {'started', 'unknown', 'pending', 'submitted'} and not _pending(old.get('result', old)):
                continue
            cfg = get_runtime_profile('mcp:' + row['profile_id'])
            if not cfg:
                continue
            # Use the principal and external identity from the original durable link.
            caller = {'id': row['principal'], 'key_type': 'live', 'scopes': ['read'], 'profile_ids': [row['profile_id']]}
            token = current_budget.set(Budget(deadline, 'background-mcp-receipt', 'read'))
            attempts = 0
            try:
                recover_unified(caller, row['profile_id'], row['operation_id']) or service._recover_adoption_receipt(caller, row['profile_id'], row['operation_id']) or service._recover_trade_receipt(caller, row['profile_id'], row['operation_id'])
                processed += 1
            except Exception:
                attempts = min((retry['attempts'] if retry else 0) + 1, 12)
            finally:
                current_budget.reset(token)
            delay = min(300, 5 * 2 ** min(max(0, attempts - 1), 6))
            with store.connection(recovery=True) as conn:
                conn.execute('INSERT INTO background_mcp_retries VALUES(?,?,?,?) ON CONFLICT(profile_id,operation_id) DO UPDATE SET attempts=excluded.attempts,next_attempt=excluded.next_attempt', (row['profile_id'], row['operation_id'], attempts, time.time() + delay))
                conn.commit()
    complete = not rows or len(rows) < limit and cursor == rows[-1]['scan_id']
    with store.connection(recovery=True) as conn:
        conn.execute("INSERT INTO background_receipt_progress VALUES('mcp',?) ON CONFLICT(worker) DO UPDATE SET cursor=excluded.cursor",
                     (0 if complete else cursor,))
        conn.commit()
    return {'processed': processed, 'scan_complete': complete, 'next_cursor': 0 if complete else cursor}
