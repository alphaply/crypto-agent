"""Durable global news snapshots, classification cache, and cross-process leases."""
from __future__ import annotations
from backend import storage

import json
import uuid
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize(conn) -> None:
    """Structure is installed by the explicit PostgreSQL migration."""


def save_processing_run(run_id: str, payload: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('''INSERT INTO news_processing_runs(run_id,started_at,updated_at,payload_json) VALUES (%s,%s,%s,%s)
            ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at,payload_json=excluded.payload_json''',
                     (run_id, payload['started_at'], now_iso(), json.dumps(payload, ensure_ascii=False)))
        conn.execute('''DELETE FROM news_processing_runs WHERE run_id NOT IN
            (SELECT run_id FROM news_processing_runs ORDER BY started_at DESC NULLS LAST LIMIT 20)''')
        conn.commit()


def processing_runs(limit: int = 20) -> dict:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        _recover_interrupted_runs(conn)
        rows = conn.execute('SELECT payload_json FROM news_processing_runs ORDER BY started_at DESC NULLS LAST LIMIT %s', (max(1, min(limit, 50)),)).fetchall()
        total = conn.execute('SELECT COUNT(*) FROM news_processing_runs').fetchone()[0]
    return {'runs': [{k: v for k, v in json.loads(row[0]).items() if k != 'items'} for row in rows], 'total': total}


def processing_run(run_id: str) -> dict | None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        _recover_interrupted_runs(conn)
        row = conn.execute('SELECT payload_json FROM news_processing_runs WHERE run_id=%s', (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def processing_calls(run_id: str) -> list[dict]:
    from backend.database import get_db_conn
    from backend.storage import record
    with get_db_conn() as conn:
        rows = conn.execute('''SELECT run_id,purpose,model,provider_id,status,started_at,
            finished_at,error FROM agent_runs WHERE config_id='news-intelligence'
            AND parent_run_id=%s ORDER BY started_at,storage_id LIMIT 650''', (run_id,)).fetchall()
    from backend.utils.log_safety import redact_log_text
    return [{**record(row), 'error': redact_log_text(row['error']) if row['error'] else None} for row in rows]


def _recover_interrupted_runs(conn) -> None:
    """An expired or replaced worker is a failed run, never a completed one."""
    # Lease acquisition and progress publication can occur in another process.
    # Read both under one write transaction so a newly acquired owner cannot be
    # mistaken for an interrupted worker using an older lease snapshot.
    storage.lock_scope(conn, 'pipeline', 'news')
    now = now_iso()
    lease = conn.execute("SELECT lease_owner,lease_until FROM shared_pipeline_state WHERE name='news'").fetchone()
    active_owner = lease['lease_owner'] if lease and (lease['lease_until'] or '') > now else None
    rows = conn.execute('SELECT run_id,payload_json FROM news_processing_runs').fetchall()
    for row in rows:
        payload = json.loads(row['payload_json'])
        if payload.get('status') == 'running' and row['run_id'] != active_owner:
            payload.update(status='error', interrupted=True, finished_at=now,
                           error='News worker was interrupted or its lease expired; previous snapshot retained')
            conn.execute('UPDATE news_processing_runs SET payload_json=%s,updated_at=%s WHERE run_id=%s',
                         (json.dumps(payload, ensure_ascii=False), now, row['run_id']))
            if lease and row['run_id'] == lease['lease_owner']:
                conn.execute("UPDATE shared_pipeline_state SET payload_json=%s,lease_owner=NULL,lease_until=NULL WHERE name='news' AND lease_owner=%s",
                             (json.dumps(payload, ensure_ascii=False), row['run_id']))
    conn.commit()


def read_state(name: str) -> dict:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        if name == 'news':
            _recover_interrupted_runs(conn)
        row = conn.execute('SELECT payload_json, lease_owner, lease_until FROM shared_pipeline_state WHERE name=%s', (name,)).fetchone()
    if not row:
        return {}
    return {**json.loads(row['payload_json']), 'running': bool(row['lease_owner'] and (row['lease_until'] or '') > now_iso())}


def update_state(name: str, updates: dict, *, owner: str | None = None, release: bool = False) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        storage.lock_scope(conn, 'pipeline', name)
        row = conn.execute('SELECT * FROM shared_pipeline_state WHERE name=%s', (name,)).fetchone()
        if owner and (not row or row['lease_owner'] != owner):
            return False
        payload = {**(json.loads(row['payload_json']) if row else {}), **updates}
        conn.execute('INSERT INTO shared_pipeline_state(name,payload_json) VALUES (%s,%s) ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json', (name, json.dumps(payload, ensure_ascii=False)))
        if release:
            conn.execute('UPDATE shared_pipeline_state SET lease_owner=NULL,lease_until=NULL WHERE name=%s', (name,))
        conn.commit()
    return True


def acquire_lease(name: str, *, seconds: int = 600, due_before: str | None = None) -> str | None:
    from backend.database import get_db_conn
    now = now_iso()
    owner = uuid.uuid4().hex
    until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    with get_db_conn() as conn:
        initialize(conn)
        storage.lock_scope(conn, 'pipeline', name)
        row = conn.execute('SELECT * FROM shared_pipeline_state WHERE name=%s', (name,)).fetchone()
        if row:
            if row['lease_owner'] and (row['lease_until'] or '') > now:
                return None
            state = json.loads(row['payload_json'])
            if due_before and (state.get('last_attempt_at') or '') > due_before:
                return None
        conn.execute('INSERT INTO shared_pipeline_state(name,lease_owner,lease_until) VALUES (%s,%s,%s) ON CONFLICT(name) DO UPDATE SET lease_owner=excluded.lease_owner,lease_until=excluded.lease_until', (name, owner, until))
        conn.commit()
    return owner


def renew_lease(name: str, owner: str, seconds: int = 120) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        result = conn.execute('UPDATE shared_pipeline_state SET lease_until=%s WHERE name=%s AND lease_owner=%s', ((datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(), name, owner))
        conn.commit()
        return result.rowcount == 1


def latest_snapshot() -> dict | None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        row = conn.execute('SELECT id, payload_json FROM global_news_snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return {**json.loads(row['payload_json']), 'snapshot_id': row['id']} if row else None


def latest_digest() -> dict | None:
    """Project only the published digest in PostgreSQL, never transfer the full blob."""
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        row = conn.execute('''SELECT n.id, jsonb_build_object(
            'digest', n.payload_json::jsonb->'digest',
            'as_of', n.payload_json::jsonb->'as_of',
            'summary_as_of', COALESCE(n.payload_json::jsonb->'summary_as_of', n.payload_json::jsonb->'as_of'),
            'available', n.payload_json::jsonb->'available',
            'stale', n.payload_json::jsonb->'stale') AS digest_payload,
            s.payload_json::jsonb->>'status' AS pipeline_status, s.lease_owner, s.lease_until
            FROM global_news_snapshots n LEFT JOIN shared_pipeline_state s ON s.name='news'
            ORDER BY n.id DESC LIMIT 1''').fetchone()
    if row is None:
        return None
    payload = row['digest_payload']
    status = row['pipeline_status'] or 'success'
    if status == 'running' and (not row['lease_owner'] or (row['lease_until'] or '') <= now_iso()):
        status = 'error'
    # read_state('news') repairs interrupted runs under a write lock. A tool
    # read must never acquire that lock or load the processing-run payloads.
    return {**(json.loads(payload) if isinstance(payload, str) else payload), 'snapshot_id': row['id'], 'status': status}


def publish_events(items: list[dict], owner: str) -> bool:
    """Publish trigger observations before summaries, under the news lease."""
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        storage.lock_scope(conn, 'pipeline', 'news')
        lease = conn.execute("SELECT lease_owner,lease_until FROM shared_pipeline_state WHERE name='news'").fetchone()
        if not lease or lease['lease_owner'] != owner or (lease['lease_until'] or '') < now_iso():
            return False
        payload = {'as_of': now_iso(), 'trigger_items': items, 'snapshot_id': owner}
        conn.execute('INSERT INTO shared_pipeline_state(name,payload_json) VALUES (%s,%s) ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json',
                     ('news-events', json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    return True


def publish_snapshot(payload: dict, owner: str) -> bool:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        storage.lock_scope(conn, 'runtime-config')
        storage.lock_scope(conn, 'pipeline', 'news')
        lease = conn.execute('SELECT lease_owner, lease_until FROM shared_pipeline_state WHERE name=%s', ('news',)).fetchone()
        if not lease or lease['lease_owner'] != owner or (lease['lease_until'] or '') < now_iso():
            return False
        from backend.database.polymarket import publish_review
        snapshot_id = conn.execute('INSERT INTO global_news_snapshots(created_at,payload_json) VALUES (%s,%s) RETURNING id', (now_iso(), json.dumps(payload, ensure_ascii=False))).fetchone()[0]
        if not publish_review(conn, owner, snapshot_id):
            conn.execute('DELETE FROM global_news_snapshots WHERE id=%s', (snapshot_id,))
            conn.commit()
            return False
        conn.execute('DELETE FROM global_news_snapshots WHERE id NOT IN (SELECT id FROM global_news_snapshots ORDER BY id DESC LIMIT 168)')
        conn.commit()
    return True


def get_score(fingerprint: str) -> dict | None:
    from backend.database import get_db_conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with get_db_conn() as conn:
        initialize(conn)
        row = conn.execute('SELECT payload_json FROM news_score_cache WHERE fingerprint=%s AND created_at>=%s', (fingerprint, cutoff)).fetchone()
    return json.loads(row[0]) if row else None


def save_score(fingerprint: str, payload: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('INSERT INTO news_score_cache(fingerprint,created_at,payload_json) VALUES (%s,%s,%s) ON CONFLICT(fingerprint) DO UPDATE SET created_at=excluded.created_at,payload_json=excluded.payload_json', (fingerprint, now_iso(), json.dumps(payload, ensure_ascii=False)))
        conn.execute('DELETE FROM news_score_cache WHERE created_at<%s', ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
        conn.commit()


def save_announcement(item: dict) -> None:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        conn.execute('INSERT INTO binance_announcements(id,published_at,payload_json) VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING', (item['id'], item['published_at'], json.dumps(item, ensure_ascii=False)))
        conn.execute('DELETE FROM binance_announcements WHERE published_at<%s', ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))
        conn.commit()


def announcements(since: str) -> list[dict]:
    from backend.database import get_db_conn
    with get_db_conn() as conn:
        initialize(conn)
        rows = conn.execute('SELECT payload_json FROM binance_announcements WHERE published_at>=%s ORDER BY published_at DESC LIMIT 300', (since,)).fetchall()
    return [json.loads(row[0]) for row in rows]
