"""PostgreSQL persistence for MCP grants, profiles and durable call receipts."""
from __future__ import annotations
from backend.storage import record as storage_record
from backend.storage import lock_scope

import hashlib
import json
import time
import threading
from contextlib import contextmanager

from backend import database

_storage_lock = threading.Lock()
_storage_gate = None
_storage_capacity = None
_storage_active = 0
_storage_queued = 0


def storage_diagnostics():
    with _storage_lock:
        return {'active': _storage_active, 'queued': _storage_queued, 'capacity': _storage_capacity}


@contextmanager
def storage_slot(recovery=False):
    from .resilience import current_budget, remaining, setting
    from backend.utils.runtime_capacity import capacity_limits_enabled
    global _storage_gate, _storage_capacity, _storage_active, _storage_queued
    if current_budget.get() is None:
        yield
        return
    limits_enabled = capacity_limits_enabled()
    with _storage_lock:
        if _storage_gate is None:
            _storage_capacity = int(setting('STORAGE_CONCURRENCY', 2, 1, 32))
            _storage_gate = threading.BoundedSemaphore(_storage_capacity)
        if limits_enabled:
            _storage_queued += 1
        else:
            _storage_active += 1
    if not limits_enabled:
        try:
            if not recovery:
                remaining('storage_queue')
            yield
        finally:
            with _storage_lock:
                _storage_active -= 1
        return
    acquired = False
    try:
        wait = min(setting('DB_WAIT_SECONDS', .25), 1 if recovery else remaining('storage_queue'))
        acquired = _storage_gate.acquire(timeout=wait)
        if not acquired:
            raise ValueError('MCP storage busy: queue timeout')
    finally:
        with _storage_lock:
            _storage_queued -= 1
            if acquired:
                _storage_active += 1
    try:
        yield
    finally:
        with _storage_lock:
            _storage_active -= 1
        _storage_gate.release()


def initialize_schema(conn):
    """Perform startup data cleanup; table DDL belongs to explicit migrations."""
    conn.execute("DELETE FROM mcp_records WHERE kind='key_lookup' AND payload::jsonb->>'id' IN "
                 "(SELECT id FROM mcp_records WHERE kind='key' AND payload::jsonb->>'revoked' IN ('true','1'))")
    conn.execute("DELETE FROM mcp_records WHERE kind='key' AND payload::jsonb->>'revoked' IN ('true','1')")


@contextmanager
def connection(*, recovery=False):
    from .resilience import remaining, setting, stage
    timeout = min(setting('DB_WAIT_SECONDS', 0.25), 1 if recovery else remaining('storage'))
    with storage_slot(recovery), database.get_db_conn(timeout=timeout) as conn:
        if recovery:
            yield conn
        else:
            with stage('storage'):
                yield conn


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def get(kind: str, key: str):
    with connection() as conn:
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind=%s AND id=%s", (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def put(kind: str, key: str, payload: dict):
    with connection() as conn:
        conn.execute('INSERT INTO mcp_records(kind,id,payload) VALUES(%s,%s,%s) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload',
                     (kind, key, json.dumps(payload, ensure_ascii=False)))
        conn.commit()


def pop(kind: str, key: str):
    with connection() as conn:
        lock_scope(conn, 'mcp-record', kind, key)
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind=%s AND id=%s", (kind, key)).fetchone()
        if kind == 'profile':
            from backend.utils.position_management import assert_configuration_claims
            assert_configuration_claims(conn, 'mcp:' + key)
        conn.execute("DELETE FROM mcp_records WHERE kind=%s AND id=%s", (kind, key))
        conn.commit()
    return json.loads(row[0]) if row else None


def delete_api_key(key_id: str) -> bool:
    """Delete a credential and its authentication lookups in one transaction."""
    with connection() as conn:
        lock_scope(conn, 'mcp-record', 'key', key_id)
        result = conn.execute("DELETE FROM mcp_records WHERE kind='key' AND id=%s", (key_id,))
        conn.execute("DELETE FROM mcp_records WHERE kind='key_lookup' AND (payload::jsonb->>'id')=%s",
                     (key_id,))
        conn.commit()
    return result.rowcount > 0


def list_records(kind: str):
    with connection() as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM mcp_records WHERE kind=%s ORDER BY id", (kind,))]


def audit(principal, tool, status, *, profile_id=None, operation_id=None, request=None, result=None):
    with connection() as conn:
        conn.execute("INSERT INTO mcp_audit(timestamp,principal,profile_id,tool,operation_id,status,request,result) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                     (time.time(), principal, profile_id, tool, operation_id, status,
                      json.dumps(request, ensure_ascii=False, default=str), json.dumps(result, ensure_ascii=False, default=str)))
        conn.commit()


def audit_rows(limit=100):
    with connection() as conn:
        return [storage_record(row) for row in conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT %s", (limit,))]


def reserve_operation(profile_id, operation_id, principal, request):
    fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    with connection() as conn:
        lock_scope(conn, 'mcp-operation', profile_id, operation_id)
        row = conn.execute("SELECT * FROM mcp_operations WHERE profile_id=%s AND operation_id=%s", (profile_id, operation_id)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise ValueError("operation_id already belongs to a different request")
            if row['principal'] != principal:
                raise PermissionError("operation_id belongs to another MCP caller")
            return json.loads(row['result']) if row['result'] else {
                "status": "unknown", "operation_id": operation_id,
                "message": "This operation was already started; inspect its orders before any further action."}
        now = time.time()
        conn.execute('INSERT INTO mcp_operations(profile_id,operation_id,fingerprint,principal,state,result,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (profile_id, operation_id, fingerprint, principal, 'started', None, now, now))
        conn.execute('INSERT INTO mcp_operation_requests(profile_id,operation_id,contract_version,request_json) VALUES(%s,%s,%s,%s)', (
            profile_id, operation_id, request.get('contract_version', 1),
            json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':'))))
        conn.execute('INSERT INTO mcp_operation_links(profile_id,operation_id,principal,internal_operation_id,symbol) VALUES(%s,%s,%s,%s,%s)',
                     (profile_id, operation_id, principal, 'mcp:' + digest(principal + ':' + operation_id), request['symbol']))
        conn.commit()
    return None


def complete_operation(profile_id, operation_id, result):
    with connection(recovery=True) as conn:
        lock_scope(conn, 'mcp-operation', profile_id, operation_id)
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        old = conn.execute('SELECT revision,result FROM mcp_operation_revisions WHERE profile_id=%s AND operation_id=%s '
                           'ORDER BY revision DESC LIMIT 1', (profile_id, operation_id)).fetchone()
        if old is None:
            initial = conn.execute('SELECT result,updated_at FROM mcp_operations WHERE profile_id=%s AND operation_id=%s',
                                   (profile_id, operation_id)).fetchone()
            if initial and initial['result'] and initial['result'] != encoded:
                conn.execute('INSERT INTO mcp_operation_revisions(profile_id,operation_id,revision,observed_at,result) VALUES(%s,%s,%s,%s,%s)',
                             (profile_id, operation_id, 0, initial['updated_at'], initial['result']))
                old = {'revision': 0, 'result': initial['result']}
        if old is None or old['result'] != encoded:
            conn.execute('INSERT INTO mcp_operation_revisions(profile_id,operation_id,revision,observed_at,result) VALUES(%s,%s,%s,%s,%s)',
                         (profile_id, operation_id, old['revision'] + 1 if old else 0, time.time(), encoded))
        conn.execute("UPDATE mcp_operations SET state=%s,result=%s,updated_at=%s WHERE profile_id=%s AND operation_id=%s",
                     (str(result.get('status') or 'completed'), json.dumps(result, ensure_ascii=False, default=str), time.time(), profile_id, operation_id))
        conn.commit()


def audit_safely(*args, **kwargs):
    """Telemetry failure must never overwrite a financial result."""
    try:
        audit(*args, **kwargs)
    except Exception as exc:
        import logging
        logging.getLogger('MCPResilience').warning('mcp_audit_failed layer=storage type=%s', type(exc).__name__)
