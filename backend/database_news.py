"""Durable global news snapshots, classification cache, and cross-process leases."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(conn) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS shared_pipeline_state (
            name TEXT PRIMARY KEY, payload_json TEXT NOT NULL DEFAULT '{}',
            lease_owner TEXT, lease_until TEXT
        );
        CREATE TABLE IF NOT EXISTS global_news_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news_score_cache (
            fingerprint TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS binance_announcements (
            id TEXT PRIMARY KEY, published_at TEXT NOT NULL, payload_json TEXT NOT NULL
        );
    ''')


def read_state(name: str) -> dict:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        row = conn.execute('SELECT payload_json, lease_owner, lease_until FROM shared_pipeline_state WHERE name=?', (name,)).fetchone()
    if not row:
        return {}
    return {**json.loads(row['payload_json']), 'running': bool(row['lease_owner'] and (row['lease_until'] or '') > now_iso())}


def update_state(name: str, updates: dict, *, owner: str | None = None, release: bool = False) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM shared_pipeline_state WHERE name=?', (name,)).fetchone()
        if owner and (not row or row['lease_owner'] != owner):
            return False
        payload = {**(json.loads(row['payload_json']) if row else {}), **updates}
        conn.execute('INSERT INTO shared_pipeline_state(name,payload_json) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json', (name, json.dumps(payload, ensure_ascii=False)))
        if release:
            conn.execute('UPDATE shared_pipeline_state SET lease_owner=NULL,lease_until=NULL WHERE name=?', (name,))
        conn.commit()
    return True


def acquire_lease(name: str, *, seconds: int = 600, due_before: str | None = None) -> str | None:
    from backend.database import get_db_conn
    now = now_iso()
    owner = uuid.uuid4().hex
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM shared_pipeline_state WHERE name=?', (name,)).fetchone()
        if row:
            if row['lease_owner'] and (row['lease_until'] or '') > now:
                return None
            state = json.loads(row['payload_json'])
            if due_before and (state.get('last_attempt_at') or '') > due_before:
                return None
        conn.execute('INSERT INTO shared_pipeline_state(name,lease_owner,lease_until) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET lease_owner=excluded.lease_owner,lease_until=excluded.lease_until', (name, owner, until))
        conn.commit()
    return owner


def renew_lease(name: str, owner: str, seconds: int = 120) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        result = conn.execute('UPDATE shared_pipeline_state SET lease_until=? WHERE name=? AND lease_owner=?', ((datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(), name, owner))
        conn.commit()
        return result.rowcount == 1


def latest_snapshot() -> dict | None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        row = conn.execute('SELECT id, payload_json FROM global_news_snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return {**json.loads(row['payload_json']), 'snapshot_id': row['id']} if row else None


def publish_snapshot(payload: dict, owner: str) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('BEGIN IMMEDIATE')
        lease = conn.execute('SELECT lease_owner, lease_until FROM shared_pipeline_state WHERE name=?', ('news',)).fetchone()
        if not lease or lease['lease_owner'] != owner or (lease['lease_until'] or '') < now_iso():
            return False
        conn.execute('INSERT INTO global_news_snapshots(created_at,payload_json) VALUES (?,?)', (now_iso(), json.dumps(payload, ensure_ascii=False)))
        conn.execute('DELETE FROM global_news_snapshots WHERE id NOT IN (SELECT id FROM global_news_snapshots ORDER BY id DESC LIMIT 168)')
        conn.commit()
    return True


def get_score(fingerprint: str) -> dict | None:
    from backend.database import get_db_conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with get_db_conn() as conn:
        initialize(conn)
        row = conn.execute('SELECT payload_json FROM news_score_cache WHERE fingerprint=? AND created_at>=?', (fingerprint, cutoff)).fetchone()
    return json.loads(row[0]) if row else None


def save_score(fingerprint: str, payload: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('INSERT OR REPLACE INTO news_score_cache VALUES (?,?,?)', (fingerprint, now_iso(), json.dumps(payload, ensure_ascii=False)))
        conn.execute('DELETE FROM news_score_cache WHERE created_at<?', ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
        conn.commit()


def save_announcement(item: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('INSERT OR IGNORE INTO binance_announcements VALUES (?,?,?)', (item['id'], item['published_at'], json.dumps(item, ensure_ascii=False)))
        conn.execute('DELETE FROM binance_announcements WHERE published_at<?', ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
        conn.commit()


def announcements(since: str) -> list[dict]:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        rows = conn.execute('SELECT payload_json FROM binance_announcements WHERE published_at>=? ORDER BY published_at DESC LIMIT 300', (since,)).fetchall()
    return [json.loads(row[0]) for row in rows]
