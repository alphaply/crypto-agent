"""SQLite persistence for MCP grants, profiles and durable call receipts."""
from __future__ import annotations

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
    global _storage_gate, _storage_capacity, _storage_active, _storage_queued
    if current_budget.get() is None:
        yield
        return
    with _storage_lock:
        if _storage_gate is None:
            _storage_capacity = int(setting('STORAGE_CONCURRENCY', 2, 1, 32))
            _storage_gate = threading.BoundedSemaphore(_storage_capacity)
        _storage_queued += 1
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
    """Startup migration; execute statements without committing caller state."""
    schema = """
            CREATE TABLE IF NOT EXISTS mcp_records (
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS mcp_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                principal TEXT NOT NULL, profile_id TEXT, tool TEXT NOT NULL,
                operation_id TEXT, status TEXT NOT NULL, request TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS mcp_operations (
                profile_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, principal TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(profile_id,operation_id));
            CREATE TABLE IF NOT EXISTS mcp_operation_requests (
                profile_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                contract_version INTEGER NOT NULL, request_json TEXT NOT NULL,
                PRIMARY KEY(profile_id,operation_id));
            CREATE TABLE IF NOT EXISTS mcp_operation_links (
                profile_id TEXT NOT NULL, operation_id TEXT NOT NULL, principal TEXT NOT NULL,
                internal_operation_id TEXT NOT NULL, symbol TEXT NOT NULL,
                PRIMARY KEY(profile_id,operation_id,principal));
            CREATE TABLE IF NOT EXISTS mcp_operation_revisions (
                profile_id TEXT NOT NULL, operation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                observed_at REAL NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(profile_id,operation_id,revision));
        """
    for statement in schema.split(';'):
        if statement.strip():
            conn.execute(statement)
    from .paper_schema import initialize_schema as initialize_paper_schema
    initialize_paper_schema(conn)


@contextmanager
def connection(*, recovery=False):
    from .resilience import remaining, setting, stage
    with storage_slot(recovery), database.get_db_conn() as conn:
        timeout_ms = max(1, int(min(setting('DB_WAIT_SECONDS', 0.25), 1 if recovery else remaining('storage')) * 1000))
        conn.execute(f'PRAGMA busy_timeout={timeout_ms}')
        if recovery:
            yield conn
        else:
            with stage('storage'):
                yield conn


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def get(kind: str, key: str):
    with connection() as conn:
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind=? AND id=?", (kind, key)).fetchone()
    return json.loads(row[0]) if row else None


def put(kind: str, key: str, payload: dict):
    with connection() as conn:
        conn.execute("INSERT INTO mcp_records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                     (kind, key, json.dumps(payload, ensure_ascii=False)))
        conn.commit()


def pop(kind: str, key: str):
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM mcp_records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if kind == 'profile':
            from backend.utils.position_management import assert_configuration_claims
            assert_configuration_claims(conn, 'mcp:' + key)
        conn.execute("DELETE FROM mcp_records WHERE kind=? AND id=?", (kind, key))
        conn.commit()
    return json.loads(row[0]) if row else None


def list_records(kind: str):
    with connection() as conn:
        return [json.loads(row[0]) for row in conn.execute("SELECT payload FROM mcp_records WHERE kind=? ORDER BY id", (kind,))]


def audit(principal, tool, status, *, profile_id=None, operation_id=None, request=None, result=None):
    with connection() as conn:
        conn.execute("INSERT INTO mcp_audit(timestamp,principal,profile_id,tool,operation_id,status,request,result) VALUES(?,?,?,?,?,?,?,?)",
                     (time.time(), principal, profile_id, tool, operation_id, status,
                      json.dumps(request, ensure_ascii=False, default=str), json.dumps(result, ensure_ascii=False, default=str)))
        conn.commit()


def audit_rows(limit=100):
    with connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (limit,))]


def reserve_operation(profile_id, operation_id, principal, request):
    fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM mcp_operations WHERE profile_id=? AND operation_id=?", (profile_id, operation_id)).fetchone()
        if row:
            if row['fingerprint'] != fingerprint:
                raise ValueError("operation_id already belongs to a different request")
            if row['principal'] != principal:
                raise PermissionError("operation_id belongs to another MCP caller")
            return json.loads(row['result']) if row['result'] else {
                "status": "unknown", "operation_id": operation_id,
                "message": "This operation was already started; inspect its orders before any further action."}
        now = time.time()
        conn.execute("INSERT INTO mcp_operations VALUES(?,?,?,?,?,?,?,?)", (profile_id, operation_id, fingerprint, principal, 'started', None, now, now))
        conn.execute('INSERT INTO mcp_operation_requests VALUES(?,?,?,?)', (
            profile_id, operation_id, request.get('contract_version', 1),
            json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':'))))
        conn.execute('INSERT INTO mcp_operation_links VALUES(?,?,?,?,?)',
                     (profile_id, operation_id, principal, 'mcp:' + digest(principal + ':' + operation_id), request['symbol']))
        conn.commit()
    return None


def complete_operation(profile_id, operation_id, result):
    with connection(recovery=True) as conn:
        conn.execute('BEGIN IMMEDIATE')
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        old = conn.execute('SELECT revision,result FROM mcp_operation_revisions WHERE profile_id=? AND operation_id=? '
                           'ORDER BY revision DESC LIMIT 1', (profile_id, operation_id)).fetchone()
        if old is None:
            initial = conn.execute('SELECT result,updated_at FROM mcp_operations WHERE profile_id=? AND operation_id=?',
                                   (profile_id, operation_id)).fetchone()
            if initial and initial['result'] and initial['result'] != encoded:
                conn.execute('INSERT INTO mcp_operation_revisions VALUES(?,?,?,?,?)',
                             (profile_id, operation_id, 0, initial['updated_at'], initial['result']))
                old = {'revision': 0, 'result': initial['result']}
        if old is None or old['result'] != encoded:
            conn.execute('INSERT INTO mcp_operation_revisions VALUES(?,?,?,?,?)',
                         (profile_id, operation_id, old['revision'] + 1 if old else 0, time.time(), encoded))
        conn.execute("UPDATE mcp_operations SET state=?,result=?,updated_at=? WHERE profile_id=? AND operation_id=?",
                     (str(result.get('status') or 'completed'), json.dumps(result, ensure_ascii=False, default=str), time.time(), profile_id, operation_id))
        conn.commit()


def audit_safely(*args, **kwargs):
    """Telemetry failure must never overwrite a financial result."""
    try:
        audit(*args, **kwargs)
    except Exception as exc:
        import logging
        logging.getLogger('MCPResilience').warning('mcp_audit_failed layer=storage type=%s', type(exc).__name__)
