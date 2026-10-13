"""Read-only recovery of an original trade operation; never replay its callback."""
from __future__ import annotations

import json
import time

from backend.utils.trade_receipts import project_receipt
from backend.utils.trade_operations import _decode_receipt, _operation_matches, _same_symbol, tool_result_status


def operation_evidence(config_id: str, symbol: str, operation_ids, *, account_scope: str) -> dict:
    """Exact referenced receipts, independent of age/page; scope comes from the caller.

    A child's identity is resolved only through a stored batch item. Similar sizes,
    prices and an unrelated flat position are never evidence of its execution.
    """
    from backend.database import get_db_conn
    ids = list(dict.fromkeys(str(i) for i in operation_ids if i))
    output = {i: {'resolution': 'unresolved', 'operation_id': i} for i in ids}
    if not config_id or not symbol or not account_scope:
        return {i: {**v, 'resolution': 'unavailable'} for i, v in output.items()}
    try:
        with get_db_conn() as conn:
            rows = conn.execute('SELECT * FROM trade_action_runs WHERE config_id=?', (config_id,)).fetchall()
            evidence = conn.execute('SELECT operation_id,account_scope,symbol,version FROM trade_operation_evidence '
                                    'WHERE config_id=?', (config_id,)).fetchall()
        refreshed = {}
        for identity in ids:
            # A refreshed parent can contain newer evidence than the child's
            # original run row. Parent candidates still require an exact item.
            candidates = sorted((r for r in rows if _same_symbol(r['symbol'], symbol)
                                 and (r['operation_id'] == identity or identity.startswith(r['operation_id'] + ':'))),
                                key=lambda r: (r['updated_at'], len(r['operation_id'])), reverse=True)
            for row in candidates:
                receipt = _decode_receipt(row['result'])
                if not isinstance(receipt, dict):
                    continue
                parent = row['operation_id']
                if parent != identity:
                    item = next((item for n, item in enumerate(receipt.get('results', []))
                                 if identity == f'{parent}:{item.get("index", n)}'), None)
                    if item is None:
                        continue
                    receipt = item if item.get('status') == 'not_executed' else item.get('result', item)
                associated = [e for e in evidence if _same_symbol(e['symbol'], symbol)
                              and _operation_matches(e['operation_id'], identity)]
                # Unsubmitted batch tails inherit the parent's scope, never execution.
                if not associated:
                    associated = [e for e in evidence if _same_symbol(e['symbol'], symbol)
                                  and _operation_matches(e['operation_id'], parent)]
                if associated and any(e['account_scope'] != account_scope for e in associated):
                    output[identity] = {**output[identity], 'resolution': 'account_scope_mismatch'}
                    break
                if not associated and not account_scope.startswith('mock:'):
                    output[identity] = {**output[identity], 'resolution': 'unavailable'}
                    break
                if associated:
                    if parent not in refreshed:
                        from backend.utils.position_protection import PositionProtection
                        service = object.__new__(PositionProtection)
                        service.config_id, service.account_scope = config_id, account_scope
                        refreshed[parent] = recover_operation(config_id, symbol, parent, service=service, read_exchange=False)
                    fresh = refreshed[parent]
                    if isinstance(fresh, dict):
                        if parent == identity:
                            receipt = fresh
                        else:
                            item = next((item for n, item in enumerate(fresh.get('results', []))
                                         if identity == f'{parent}:{item.get("index", n)}'), None)
                            if item:
                                receipt = item if item.get('status') == 'not_executed' else item.get('result', item)
                output[identity] = {'operation_id': identity, 'parent_operation_id': parent,
                                    'resolution': 'matched', 'latest': receipt,
                                    'source': 'local_execution_evidence',
                                    'evidence_version': max((e['version'] for e in associated), default=None),
                                    'receipt_updated_at': row['updated_at']}
                break
    except Exception as exc:
        output = {i: {**v, 'resolution': 'unavailable', 'error': str(exc)} for i, v in output.items()}
    return output


def _stale_advice(continuation):
    return {**continuation, 'assessment_state': 'needs_refresh', 'rules': [], 'blocked_actions': [],
            'requires_action_assessment': True, 'previous_assessment': continuation}


def latest_operation_receipts(config_id: str, symbol: str = '', limit: int = 20) -> dict:
    """Local observations for decision/UI context, never a new exchange request."""
    from backend.database import get_db_conn
    if not config_id:
        return {}
    with get_db_conn() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_action_runs'").fetchone():
            return {}
        rows = conn.execute('SELECT operation_id,symbol,result,updated_at FROM trade_action_runs WHERE config_id=? '
                            'AND result IS NOT NULL ORDER BY updated_at DESC LIMIT ?', (config_id, limit)).fetchall()
    result = {}
    for row in rows:
        value = _decode_receipt(row['result'])
        if (not symbol or _same_symbol(row['symbol'], symbol)) and isinstance(value, dict) and value.get('receipt_version') == 1:
            result[row['operation_id']] = {**value, 'receipt_updated_at': row['updated_at']}
    return result


def recovery_context_text(config_id: str, symbol: str) -> str:
    receipts = latest_operation_receipts(config_id, symbol)
    return ('\n\n[当前交易核验记录]\n' + json.dumps(receipts, ensure_ascii=False) +
            '\n这些是历史操作与缓存核验结果。查询失败或needs_refresh不代表交易被禁止；'
            '有新的仓位/挂单管理意图应直接调用对应工具，由工具在调用内核验。'
            '不得仅凭旧限制宣称本轮无法交易，也不得换ID重发同一笔未知写入。') if receipts else ''


def _intent(request: dict, operation_id: str, evidence_id: str) -> str:
    if request.get('action') in {'open', 'close', 'edit', 'cancel'}:
        return request['action']
    tool = request.get('tool', '')
    args = request.get('args', request)
    if tool in {'open', 'close', 'edit', 'cancel'}:
        return tool
    actions = args.get('actions') or args.get('orders') or []
    suffix = evidence_id[len(operation_id):].lstrip(':').split(':')[0]
    if suffix.isdigit() and int(suffix) < len(actions):
        action = actions[int(suffix)].get('action', '')
        if action in {'open', 'close', 'edit', 'cancel'}:
            return action
    if tool.startswith('cancel'):
        return 'cancel'
    if tool.startswith('update'):
        return 'edit'
    return 'close' if tool.startswith('close') else 'open'


def _records(plan, operation_id):
    for record in plan.get('entries', []) + plan.get('exits', []):
        ids = [record.get(key) for key in ('operation_id', 'cancel_operation_id', 'last_operation_id')]
        ids += [(record.get(key) or {}).get('operation_id') for key in ('amendment', 'replacement')]
        for evidence_id in sorted(set(ids) - {None, ''}):
            if _operation_matches(evidence_id, operation_id):
                yield evidence_id, record


def recover_operation(config_id: str, symbol: str, operation_id: str, *, service=None, read_exchange=True):
    """Reload and repeat read-only observations after a concurrent cache update."""
    from backend.utils.position_protection import ConcurrentProtectionUpdate
    for attempt in range(3):
        try:
            return _recover_operation_once(config_id, symbol, operation_id,
                                           service=service, read_exchange=read_exchange)
        except ConcurrentProtectionUpdate:
            if not read_exchange or attempt == 2:
                raise


def _recover_operation_once(config_id: str, symbol: str, operation_id: str, *, service=None, read_exchange=True):
    from backend import database
    from backend.utils.trade_evidence import append_receipt, load_cycles, save_plan, evidence_fingerprint
    from backend.utils.position_protection import _lock, ConcurrentProtectionUpdate
    with database.get_db_conn() as conn:
        row = conn.execute('SELECT * FROM trade_action_runs WHERE config_id=? AND operation_id=?',
                           (config_id, operation_id)).fetchone()
        saved = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                             (config_id, operation_id)).fetchone()
    if not row or not _same_symbol(row['symbol'], symbol):
        return {'status': 'failed', 'error_code': 'operation_not_found', 'error': 'Original operation is not in this task and symbol'}
    previous = _decode_receipt(row['result'])
    request = json.loads(saved['request_json']) if saved else {}
    with database.get_db_conn() as conn:
        identities = conn.execute('SELECT DISTINCT operation_id FROM trade_operation_evidence WHERE config_id=?', (config_id,)).fetchall()
    if not any(_operation_matches(item['operation_id'], operation_id) for item in identities):
        return previous or {'status': 'unknown', 'operation_id': operation_id, 'error': 'Original execution evidence unavailable'}
    if service is None:
        if read_exchange:
            from backend.agent.agent_tools import _independent_service
            service, real = _independent_service(config_id, symbol)
            if not real:
                return previous or {'status': 'unknown', 'operation_id': operation_id}
        else:
            from backend.utils.position_protection import PositionProtection
            from backend.utils.trade_evidence import configured_account_scope
            # _load uses only the database. In particular, avoid MarketTool's
            # constructor, which loads exchange markets even for a local read.
            service = object.__new__(PositionProtection)
            service.config_id = config_id
            service.account_scope = configured_account_scope(config_id)
    # Do not carry a snapshot loaded before a same-process writer releases its lock.
    with _lock(service.account_scope):
        plans = load_cycles(config_id, service.account_scope, symbol, operation_id)
    if not plans:
        # No evidence in this account must not borrow a former account's result.
        with database.get_db_conn() as conn:
            other = conn.execute('SELECT 1 FROM trade_operation_evidence WHERE config_id=? AND operation_id=? '
                                 'AND account_scope!=?', (config_id, operation_id, service.account_scope)).fetchone()
        if other:
            return {'status': 'unknown', 'operation_id': operation_id, 'error_code': 'account_scope_mismatch',
                    'error': 'Original operation belongs to different exchange credentials'}
        return previous or {'status': 'unknown', 'operation_id': operation_id, 'error': 'Original execution evidence unavailable'}
    projections, query_errors = {}, []
    with _lock(service.account_scope):
        plans = load_cycles(config_id, service.account_scope, symbol, operation_id)
        fingerprint = evidence_fingerprint(plans, operation_id)
        if not read_exchange:
            with database.get_db_conn() as conn:
                watermark = conn.execute('SELECT fingerprint FROM trade_receipt_watermarks WHERE config_id=? '
                                         'AND account_scope=? AND operation_id=?',
                                         (config_id, service.account_scope, operation_id)).fetchone()
            if watermark and watermark['fingerprint'] == fingerprint:
                return previous
        for plan in plans:
            current = service._load(plan['symbol'], plan['side'])
            active = current
            historical = bool(active and active.get('episode_id') != plan.get('episode_id'))
            cycle_diagnosis = None
            if read_exchange and (not current or current.get('state') == 'DONE'):
                from backend.utils.independent_cycle_recovery import diagnose_cycle
                cycle_diagnosis, _ = diagnose_cycle(service, plan['symbol'], plan['side'], current)
            if read_exchange:
                expected = plan.get('record_version', 0)
                if historical:
                    service._collect_order_evidence(plan)
                    plan['verified_at'] = time.time()
                    plan['position_observation'] = {**plan.get('position_observation', {}),
                                                    'status': 'historical', 'episode_id': plan['episode_id']}
                else:
                    service.collect_readonly(plan)
                query_errors.extend(plan.get('read_errors', []))
                # Only observation caches change; do not invoke _save/_reconcile
                # here, since ownership, replacement and maintenance are writes.
                plan['record_version'] = expected + 1
                with database.get_db_conn() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    save_plan(conn, config_id, plan, expected_version=expected)
                    current = conn.execute('SELECT payload FROM real_protection_plans WHERE config_id=? AND symbol=? AND side=?',
                                           (config_id, plan['symbol'], plan['side'])).fetchone()
                    current_plan = json.loads(current['payload']) if current else {}
                    if current_plan.get('episode_id') == plan['episode_id']:
                        if current_plan.get('record_version', 0) != expected:
                            raise ConcurrentProtectionUpdate('Position cycle changed during read; reload observations')
                        conn.execute('UPDATE real_protection_plans SET payload=? WHERE config_id=? AND symbol=? AND side=?',
                                     (json.dumps(plan), config_id, plan['symbol'], plan['side']))
                    conn.commit()
            availability = plan
            if active and active.get('episode_id') != plan.get('episode_id'):
                availability = active
                if read_exchange:
                    service.collect_readonly(availability)
                    query_errors.extend(availability.get('read_errors', []))
            if read_exchange:
                try:
                    availability['hedged'] = bool(service.ex.fetch_position_mode(plan['symbol']).get('hedged'))
                    if not availability['hedged']:
                        opposite = service._load(plan['symbol'], 'SHORT' if plan['side'] == 'LONG' else 'LONG')
                        if opposite and opposite.get('state') != 'DONE':
                            service.collect_readonly(opposite)
                            query_errors.extend(opposite.get('read_errors', []))
                        availability['opposite_evidence'] = opposite
                except Exception as exc:
                    query_errors.append(str(exc))
                    availability = {**availability, 'position_error': 'Position mode could not be verified'}
            from backend.utils.independent_admission import continuation_for
            if read_exchange and (availability.get('adoption') or str(config_id).startswith('mcp:')):
                from backend.mcp.guard import assert_position_owner
                try:
                    assert_position_owner(service.mt, plan['symbol'], plan_override=availability,
                                          adopted_only=bool(availability.get('adoption')), side=plan['side'])
                    availability.pop('ownership_error', None)
                except Exception as exc:
                    availability['ownership_error'] = str(exc)
                    query_errors.append(str(exc))
            continuation = continuation_for(availability)
            if not read_exchange or query_errors or not active or active.get('state') == 'DONE':
                continuation = _stale_advice(continuation)
                continuation['scope'] = {'account_scope': service.account_scope, 'symbol': plan['symbol'],
                                         'side': plan['side'], 'episode_id': (active or {}).get('episode_id')}
            for evidence_id, record in _records(plan, operation_id):
                action = _intent(request, operation_id, evidence_id)
                receipt = project_receipt(plan, record, operation_id=evidence_id, action=action,
                                          error=record.get('query_error'))
                receipt['verified_at'] = plan.get('verified_at')
                receipt['continuation'] = continuation
                receipt['current_scope'] = continuation['scope']
                receipt['current_position'] = availability.get('position_observation')
                receipt['projected_at'] = time.time()
                receipt['evidence_source'] = 'exchange_read' if read_exchange else 'local_projection'
                receipt['query'] = ({'status': 'partial' if query_errors else 'completed',
                                     'queried_at': time.time(), 'observed_at': plan.get('verified_at'),
                                     'source': 'exchange_read', 'errors': list(dict.fromkeys(query_errors))}
                                    if read_exchange else {'status': 'not_requested', 'source': 'local_projection',
                                                           'observed_at': plan.get('verified_at')})
                original = previous
                if isinstance(previous, dict) and isinstance(previous.get('results'), list):
                    original = next((item.get('result') for index, item in enumerate(previous['results'])
                                     if f'{operation_id}:{item.get("index", index)}' == evidence_id), None)
                if isinstance(original, dict) and original.get('admission'):
                    receipt['admission'] = original['admission']
                if cycle_diagnosis:
                    receipt['cycle_diagnosis'] = cycle_diagnosis
                old = projections.get(evidence_id)
                # A replacement successor is stronger evidence of edit success
                # than cancellation of its predecessor.
                if old is None or record.get('replaces'):
                    projections[evidence_id] = receipt
    if not projections:
        return previous
    if operation_id in projections and (request.get('tool') in {'open', 'close', 'edit', 'cancel'} or request.get('action')):
        result = projections[operation_id]
    elif isinstance(previous, dict) and isinstance(previous.get('results'), list):
        result = dict(previous)
        items = []
        for index, original in enumerate(previous['results']):
            item = dict(original)
            evidence = projections.get(f'{operation_id}:{item.get("index", index)}')
            if evidence and item.get('status') != 'not_executed':
                item.update(status=evidence['status'], result=evidence)
            items.append(item)
        result['results'] = items
        result['query'] = next(iter(projections.values()))['query']
        result['projected_at'] = time.time()
        statuses = [tool_result_status(item) for item in items]
        result['status'] = next((s for s in ['unknown', 'pending', 'failed', 'submitted'] if s in statuses),
                                'partial' if any(i.get('status') == 'not_executed' for i in items) else 'completed')
        if result.get('execution_policy') == 'independent':
            item_statuses = [item.get('status') for item in items]
            result['status'] = next((s for s in ('unknown', 'pending', 'failed') if s in item_statuses),
                                    'failed' if 'not_executed' in item_statuses else
                                    'submitted' if 'submitted' in item_statuses else 'completed')
            result['summary'] = {s: item_statuses.count(s) for s in
                                 ('completed', 'submitted', 'failed', 'unknown', 'pending', 'not_executed', 'skipped')}
    else:
        from backend.utils.trade_operations import _merge_reconciled_result
        result = _merge_reconciled_result(row, list(projections.values()), request)
    with database.get_db_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for plan in plans:
            latest = conn.execute('SELECT version FROM trade_cycle_evidence WHERE config_id=? AND account_scope=? '
                                  'AND symbol=? AND episode_id=?', (config_id, service.account_scope, plan['symbol'], plan['episode_id'])).fetchone()
            if latest and latest['version'] != plan.get('record_version', 0):
                raise ConcurrentProtectionUpdate('Evidence advanced before receipt commit; reload observations')
        # Preserve even pre-migration receipts before writing a new projection.
        has_original = conn.execute('SELECT 1 FROM trade_receipt_revisions WHERE config_id=? AND operation_id=?',
                                    (config_id, operation_id)).fetchone()
        if not has_original and row['result']:
            append_receipt(conn, config_id, operation_id, json.loads(row['result']))
        revision = append_receipt(conn, config_id, operation_id, result)
        conn.execute('UPDATE trade_action_runs SET status=?,result=?,updated_at=? WHERE config_id=? AND operation_id=?',
                     (tool_result_status(result), json.dumps(result), time.time(), config_id, operation_id))
        conn.execute('INSERT INTO trade_receipt_watermarks VALUES(?,?,?,?) ON CONFLICT(config_id,account_scope,operation_id) '
                     'DO UPDATE SET fingerprint=excluded.fingerprint',
                     (config_id, service.account_scope, operation_id, evidence_fingerprint(plans, operation_id)))
        conn.commit()
    return result
