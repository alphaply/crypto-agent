"""Watchlist drafts, atomic publication and monotonic configuration revisions."""
from __future__ import annotations

import json
from backend import storage
from backend.database import get_db_conn
from backend.database.news import now_iso
from backend.utils.polymarket import PolymarketSettings


def begin_review(run_id: str, fallback: dict) -> dict | None:
    with get_db_conn() as conn:
        storage.lock_scope(conn, 'runtime-config')
        if conn.execute('SELECT 1 FROM polymarket_reviews WHERE run_id=%s', (run_id,)).fetchone():
            return None  # A retried round must not propose/apply a second change.
        row = conn.execute("SELECT value_json FROM app_settings WHERE key='polymarket'").fetchone()
        settings = PolymarketSettings.model_validate(json.loads(row[0]) if row else fallback).model_dump()
        if not settings['enabled'] or not settings['auto_manage']:
            return None
        revision = conn.execute('SELECT revision FROM polymarket_watchlist_revision WHERE id=1').fetchone()[0]
        conn.execute('INSERT INTO polymarket_reviews(run_id,status,before_revision,before_json,started_at) VALUES(%s,%s,%s,%s,%s)',
                     (run_id, 'running', revision, json.dumps(settings), now_iso()))
        conn.commit()
    return settings


def prepare_review(run_id: str, events: list[str], decisions: list[dict]) -> None:
    with get_db_conn() as conn:
        conn.execute("UPDATE polymarket_reviews SET status='prepared',after_json=%s,decisions_json=%s,checked_at=%s WHERE run_id=%s AND status='running'",
                     (json.dumps(events), json.dumps(decisions, ensure_ascii=False), now_iso(), run_id))
        conn.commit()


def fail_review(run_id: str, error: str, status: str = 'failed') -> None:
    with get_db_conn() as conn:
        conn.execute("UPDATE polymarket_reviews SET status=%s,error=%s,checked_at=%s WHERE run_id=%s AND status IN ('running','prepared')",
                     (status, error[:1000], now_iso(), run_id))
        conn.commit()


def publish_review(conn, run_id: str, snapshot_id: int) -> bool:
    """Caller holds runtime-config then news locks. False aborts the snapshot."""
    row = conn.execute('SELECT * FROM polymarket_reviews WHERE run_id=%s', (run_id,)).fetchone()
    if not row or row['status'] not in {'prepared', 'published', 'unchanged'}:
        return True  # Disabled or failed reviews leave the list alone.
    if row['status'] in {'published', 'unchanged'}:
        return False
    revision = conn.execute('SELECT revision FROM polymarket_watchlist_revision WHERE id=1').fetchone()[0]
    if revision != row['before_revision']:
        conn.execute("UPDATE polymarket_reviews SET status='conflict',error=%s,checked_at=%s WHERE run_id=%s",
                     ('监控设置已被修改，本轮未覆盖新设置；摘要与列表保持上次发布版本。', now_iso(), run_id))
        return False
    before, after = json.loads(row['before_json']), json.loads(row['after_json'])
    changed = before['events'] != after
    if changed:
        # Only this setting is updated; never save the entire runtime snapshot.
        conn.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES('polymarket',%s,%s) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                     (json.dumps({**before, 'events': after}), now_iso()))
    conn.execute('UPDATE polymarket_reviews SET status=%s,published_at=%s,snapshot_id=%s WHERE run_id=%s',
                 ('published' if changed else 'unchanged', now_iso(), snapshot_id, run_id))
    return True


def review_history(limit: int = 20) -> list[dict]:
    with get_db_conn() as conn:
        storage.lock_scope(conn, 'pipeline', 'news')
        conn.execute("""UPDATE polymarket_reviews SET status='interrupted',error=%s,checked_at=%s
            WHERE status IN ('running','prepared') AND NOT EXISTS
            (SELECT 1 FROM shared_pipeline_state WHERE name='news' AND lease_owner=polymarket_reviews.run_id AND lease_until>%s)""",
                     ('本轮执行中断，原监控列表已保留。', now_iso(), now_iso()))
        rows = conn.execute('SELECT * FROM polymarket_reviews ORDER BY started_at DESC LIMIT %s', (max(1, min(limit, 100)),)).fetchall()
        conn.commit()
    return [{**dict(row), 'before': json.loads(row['before_json'])['events'], 'after': json.loads(row['after_json']) if row['after_json'] else None,
             'decisions': json.loads(row['decisions_json'])} for row in rows]


def maintenance_status() -> dict:
    rows = review_history(1)
    return {key: rows[0][key] for key in ('run_id', 'status', 'checked_at', 'error')} if rows else {'status': 'not_checked'}
