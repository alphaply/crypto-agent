"""SQLite persistence for MCP grants, profiles and durable call receipts."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager

from backend import database


@contextmanager
def connection():
    with database.get_db_conn() as conn:
        conn.executescript("""
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
        """)
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
        conn.commit()
    return None


def complete_operation(profile_id, operation_id, result):
    with connection() as conn:
        conn.execute("UPDATE mcp_operations SET state=?,result=?,updated_at=? WHERE profile_id=? AND operation_id=?",
                     (str(result.get('status') or 'completed'), json.dumps(result, ensure_ascii=False, default=str), time.time(), profile_id, operation_id))
        conn.commit()
