"""Durable tool-call receipts: uncertain exchange writes are never blindly replayed."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
import math
import time
import uuid

current_operation_id: ContextVar[str | None] = ContextVar("trade_operation_id", default=None)


def _operation_matches(candidate: str | None, operation_id: str) -> bool:
    return bool(candidate) and (candidate == operation_id or str(candidate).startswith(operation_id + ':'))


def _same_symbol(left: str, right: str) -> bool:
    def normalized(value):
        value = str(value or '').upper()
        return value if ':' in value else value + ':' + value.split('/')[-1]
    return normalized(left) == normalized(right)


def _decode_receipt(value):
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value, _ = json.JSONDecoder().raw_decode(value.lstrip())
        except (ValueError, TypeError):
            break
    return value


def _merge_reconciled_result(row, observed: list[dict], request):
    """Recover only matching logical items, keeping every previously recorded outcome."""
    previous = _decode_receipt(row['result'])
    original = previous if isinstance(previous, dict) else {}
    stored_items = original.get('results')
    payload = request.get('args', request) if isinstance(request, dict) else {}
    requested_items = next((payload[key] for key in ('actions', 'orders') if isinstance(payload.get(key), list)), None)
    expected = len(requested_items) if requested_items is not None else 1
    if isinstance(stored_items, list):
        expected = max(expected, len(stored_items))
    items = []
    changed = False
    for index in range(expected):
        stored = stored_items[index] if isinstance(stored_items, list) and index < len(stored_items) else None
        item = dict(stored) if isinstance(stored, dict) else {'index': index, 'status': 'unknown'}
        if stored is not None and not isinstance(stored, dict):
            item['result'] = stored
        if tool_result_status(item) not in {'unknown'}:
            # Never turn an explicit failed / not_executed / successful result into a different outcome.
            items.append(item)
            continue
        if requested_items is not None and index < len(requested_items) and isinstance(requested_items[index], dict) and requested_items[index].get('action') is not None:
            item.setdefault('action', requested_items[index]['action'])
        child_id = f"{row['operation_id']}:{item.get('index', index)}"
        evidence = [record for record in observed if _operation_matches(record.get('operation_id'), child_id)]
        if expected == 1:
            evidence = observed
        if evidence and request is not None:
            recovered_status = tool_result_status(evidence)
            if recovered_status != 'unknown':
                original_item = dict(item)
                item = {key: value for key, value in item.items() if key not in {'status', 'error', 'result', 'results', 'pending'}}
                item.update(status=recovered_status, reconciled=True, previous_result=original_item,
                            result={'status': recovered_status, 'results': evidence, 'reconciled': True})
                changed = True
        items.append(item)
    statuses = [tool_result_status(item) for item in items]
    incomplete = any(item.get('status') == 'not_executed' for item in items)
    if request is None or 'unknown' in statuses:
        status = 'unknown'
    elif 'failed' in statuses:
        status = 'failed'
    elif incomplete:
        status = 'partial'
    else:
        status = 'submitted' if 'submitted' in statuses else 'completed'
    result = dict(original)
    if previous is not None:
        result.setdefault('original_result', previous)
    # Old exception text is historical evidence, not the current execution status.
    result.pop('error', None)
    result.pop('pending', None)
    partial = incomplete or status == 'unknown' or ('failed' in statuses and any(value in {'completed', 'submitted'} for value in statuses))
    result.update(status=status, operation_id=row['operation_id'], reconciled=changed,
                  partial_execution=partial, results=items,
                  reconciliation_evidence=observed,
                  message='逐项核验已提交动作；保留失败及未执行项，未执行动作没有重放，缺失证据仍待核验。')
    if request is None:
        result['request_metadata_missing'] = True
    return result


def _reconciled_receipt(conn, row):
    """Resolve a lost acknowledgement only from durable native execution evidence."""
    tables = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    observed = []
    if {'spot_budget_reservations', 'orders', 'spot_order_fills'} <= tables:
        records = conn.execute('''SELECT DISTINCT r.operation_id,r.symbol,r.order_id,o.amount,o.entry_price,
                f.status,f.filled_qty,f.filled_cost,f.avg_fill_price
            FROM spot_budget_reservations r JOIN orders o ON o.config_id=r.config_id
                AND o.symbol=r.symbol AND o.order_id=r.order_id
            JOIN spot_order_fills f ON f.config_id=r.config_id AND f.symbol=r.symbol AND f.order_id=r.order_id
            WHERE r.config_id=? AND r.status='submitted' AND o.trade_mode='SPOT_DCA'
                AND COALESCE(o.event_type,'ORDER_CREATED')='ORDER_CREATED'
                AND UPPER(o.side) IN ('BUY','BUY_LIMIT')''', (row['config_id'],)).fetchall()
        for record in records:
            if not _operation_matches(record['operation_id'], row['operation_id']):
                continue
            status = str(record['status'] or '').lower()
            values = [float(record[key] or 0) for key in ('filled_qty', 'filled_cost', 'avg_fill_price')]
            if (status not in {'open', 'partial', 'partially_filled', 'filled', 'closed', 'canceled', 'cancelled', 'expired', 'rejected'}
                    or any(not math.isfinite(value) or value < 0 for value in values)
                    or (values[0] > 0 and values[1] <= 0 and values[2] <= 0)):
                continue
            observed.append({'operation_id': record['operation_id'], 'symbol': record['symbol'],
                'id': record['order_id'], 'amount': record['amount'], 'price': record['entry_price'],
                'filled': values[0], 'cost': values[1],
                'status': 'open' if status in {'partial', 'partially_filled'} else status})
    if 'real_protection_plans' in tables:
        plans = conn.execute('SELECT symbol,payload FROM real_protection_plans WHERE config_id=?', (row['config_id'],)).fetchall()
        for stored in plans:
            if not _same_symbol(stored['symbol'], row['symbol']):
                continue
            try:
                plan = json.loads(stored['payload'])
                records = plan.get('entries', []) + plan.get('exits', []) + plan.get('legs', [])
                if plan.get('exit_order'):
                    records.append(plan['exit_order'])
                for record in records:
                    operation_fields = [record.get(key) for key in ('operation_id', 'cancel_operation_id', 'last_operation_id')]
                    details = [record.get(key) for key in ('amendment', 'replacement') if isinstance(record.get(key), dict)]
                    matching_details = [item for item in details if _operation_matches(item.get('operation_id'), row['operation_id'])]
                    if not any(_operation_matches(value, row['operation_id']) for value in operation_fields) and not matching_details:
                        continue
                    # A known order ID alone is not proof the requested cancel/amend succeeded.
                    if plan.get('error') or any(item.get('state') == 'pending' for item in details) or record.get('replacement'):
                        return None
                    status = str(record.get('status') or '').lower()
                    if _operation_matches(record.get('cancel_operation_id'), row['operation_id']) and status not in {'canceled', 'cancelled', 'closed', 'filled', 'expired', 'rejected'}:
                        return None
                    if not record.get('id') or status not in {'open', 'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'}:
                        return None
                    if matching_details:
                        detail_statuses = {item.get('state') for item in matching_details}
                        if not detail_statuses <= {'confirmed', 'unchanged', 'rejected'}:
                            return None
                        status = 'rejected' if 'rejected' in detail_statuses else 'confirmed'
                    evidence_ids = [detail.get('operation_id') for detail in matching_details] + list(reversed(operation_fields))
                    evidence_id = next(value for value in evidence_ids if _operation_matches(value, row['operation_id']))
                    observed.append({
                        'id': record['id'], 'status': status, 'operation_id': evidence_id,
                        'side': plan.get('side'), 'exit_type': record.get('exit_type'),
                        'price': record.get('price'), 'trigger_price': record.get('trigger_price'),
                        'amount': record.get('amount'), 'filled': record.get('filled'), 'quantity_unit': 'contracts',
                    })
            except (ValueError, TypeError, AttributeError):
                # Corrupt local evidence must never authorize a replay or report success.
                return None
    if 'mock_trade_operations' in tables:
        records = conn.execute('SELECT operation_id,symbol,result FROM mock_trade_operations WHERE config_id=?', (row['config_id'],)).fetchall()
        for record in records:
            if _operation_matches(record['operation_id'], row['operation_id']) and _same_symbol(record['symbol'], row['symbol']):
                try:
                    recovered = json.loads(record['result'])
                    if not isinstance(recovered, dict):
                        return None
                    observed.append({**recovered, 'operation_id': record['operation_id']})
                except (ValueError, TypeError):
                    return None
    if not observed:
        return None
    metadata = conn.execute('SELECT request_json FROM trade_operation_requests WHERE config_id=? AND operation_id=?',
                            (row['config_id'], row['operation_id'])).fetchone() if 'trade_operation_requests' in tables else None
    request = json.loads(metadata['request_json']) if metadata else None
    result = _merge_reconciled_result(row, observed, request)
    status = tool_result_status(result)
    conn.execute('UPDATE trade_action_runs SET status=?,result=?,updated_at=? WHERE config_id=? AND operation_id=?',
                 (status, json.dumps(result, ensure_ascii=False), time.time(), row['config_id'], row['operation_id']))
    return result


def reconcile_pending_trade_operations(config_id: str) -> int:
    """Refresh uncertain receipts from local evidence; never call an exchange or tool."""
    from backend import database
    count = 0
    with database.get_db_conn() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_action_runs'").fetchone():
            return 0
        rows = conn.execute("SELECT * FROM trade_action_runs WHERE config_id=? AND status IN ('running','unknown','pending')", (config_id,)).fetchall()
        for row in rows:
            if _reconciled_receipt(conn, row) is not None:
                count += 1
        conn.commit()
    return count


def initialize_trade_operations_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS trade_action_runs (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, symbol TEXT NOT NULL,
        request_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(config_id,operation_id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS trade_operation_requests (
        config_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_json TEXT NOT NULL,
        PRIMARY KEY(config_id,operation_id))""")


def tool_result_status(result) -> str:
    if isinstance(result, str):
        try:
            # Compatibility with legacy tool responses containing JSON plus an explanation.
            parsed, _ = json.JSONDecoder().raw_decode(result.lstrip())
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return tool_result_status(parsed)
        lower = result.lower()
        if any(s in lower for s in ("❌", "error:", "failed", "失败", "insufficient", "blocked", "rejected")):
            return "unknown" if any(s in lower for s in ("timeout", "timed out", "network", "未知", "pending", "待核验", "未确认")) else "failed"
        if any(s in lower for s in ("pending", "unknown", "待核验", "未知", "未确认", "待核对")):
            return "unknown"
        if any(s in result for s in ("[Skip]", "[跳过]", "[Duplicate]")):
            return "skipped"
        if any(s in result for s in ("委托已提交", "下单成功", "Executed Strategy")):
            return "submitted"
        return "completed"
    if isinstance(result, list):
        statuses = [tool_result_status(item) for item in result]
        return next((status for status in ('unknown', 'failed', 'submitted') if status in statuses),
                    'skipped' if statuses and all(status == 'skipped' for status in statuses) else 'completed')
    if isinstance(result, dict):
        status = str(result.get("status") or "").lower()
        if status == 'not_executed':
            return 'skipped'
        if status == 'partial':
            return 'failed'
        amendment_detail = result.get('amendment') or {}
        amendment = str(result.get('amendment_state') or (amendment_detail.get('state') if isinstance(amendment_detail, dict) else '') or '').lower()
        nested = result.get('results')
        if isinstance(nested, list) and nested:
            nested_status = tool_result_status(nested)
            if nested_status in {'unknown', 'failed'}:
                return nested_status
        else:
            nested_status = None
        if amendment == 'pending' or result.get('pending') is True:
            return 'unknown'
        if amendment in {'rejected', 'failed'}:
            return 'failed'
        if status in {"failed", "error", "rejected"}:
            return "failed"
        if status in {"unknown", "pending", "submitting", "running", "amending", "canceling"}:
            return "unknown"
        if status in {"completed", "filled", "closed", "cancelled", "canceled", "expired", "skipped", "confirmed", "unchanged"}:
            if nested_status == 'submitted':
                return 'submitted'
            return "completed" if status != "skipped" else "skipped"
        if result.get("error"):
            return "unknown"
        if amendment in {'confirmed', 'unchanged'}:
            return 'completed'
        if nested_status:
            return nested_status
        if result.get('success') is False:
            return 'failed'
        return "submitted"
    return "unknown"


def run_once(config_id: str, symbol: str, operation_id: str | None, request: dict, callback):
    from backend import database
    operation_id = operation_id or uuid.uuid4().hex
    encoded = json.dumps(request, sort_keys=True, ensure_ascii=False, default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with database.get_db_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        initialize_trade_operations_schema(conn.cursor())
        row = conn.execute("SELECT * FROM trade_action_runs WHERE config_id=? AND operation_id=?", (config_id, operation_id)).fetchone()
        if row:
            if row["request_hash"] != fingerprint or row["symbol"] != symbol:
                raise ValueError("操作 ID 已用于不同请求；禁止重用")
            if row['status'] in {'running', 'unknown', 'pending'}:
                refreshed = _reconciled_receipt(conn, row)
                if refreshed is not None:
                    conn.commit()
                    return refreshed
            if row["result"]:
                return json.loads(row["result"])
            return {"status": "unknown", "operation_id": operation_id, "error": "上次操作未确认，需核验实际订单；不会重复提交"}
        now = time.time()
        conn.execute("INSERT INTO trade_action_runs VALUES(?,?,?,?,?,?,?,?)", (config_id, operation_id, symbol, fingerprint, "running", None, now, now))
        conn.execute('INSERT INTO trade_operation_requests VALUES(?,?,?)', (config_id, operation_id, encoded))
        conn.commit()
    token = current_operation_id.set(operation_id)
    try:
        result = callback()
    except Exception as exc:
        # Once execution starts, exceptions may occur after the exchange accepted a write.
        result = {"status": "unknown", "operation_id": operation_id, "error": str(exc)}
    finally:
        current_operation_id.reset(token)
    status = tool_result_status(result)
    with database.get_db_conn() as conn:
        conn.execute("UPDATE trade_action_runs SET status=?,result=?,updated_at=? WHERE config_id=? AND operation_id=?", (status, json.dumps(result, ensure_ascii=False, default=str), time.time(), config_id, operation_id))
        conn.commit()
    return result
