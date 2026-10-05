"""Serialize expensive reviews across scheduler/manual requests and app workers."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time
import json
from threading import Event, Thread
from uuid import uuid4

from backend import database
from backend.utils.logger import setup_logger

logger = setup_logger('MemoryWorkflow')
_active_lease = ContextVar('active_memory_review_lease', default=None)


def verify_memory_lease() -> None:
    lease = _active_lease.get()
    if lease is None:
        return
    with database.get_db_conn() as conn:
        valid = conn.execute('SELECT 1 FROM memory_review_leases WHERE config_id=? AND owner=? AND expires_at>?',
                             (*lease, time.time())).fetchone()
    if not valid:
        raise RuntimeError('记忆整理租约已失效，停止修改与保存')


def _initialize_results(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_review_results (
        operation_id TEXT PRIMARY KEY, config_id TEXT NOT NULL, status TEXT NOT NULL,
        summary TEXT NOT NULL, rule_receipts TEXT NOT NULL, error TEXT NOT NULL,
        created_at REAL NOT NULL)''')


def get_review_result(operation_id: str) -> dict | None:
    with database.get_db_conn() as conn:
        _initialize_results(conn)
        row = conn.execute('SELECT * FROM memory_review_results WHERE operation_id=?', (operation_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['rule_receipts'] = json.loads(result['rule_receipts'])
        return result


def save_review_result(config_id: str, operation_id: str, result) -> None:
    """Persist terminal outcomes independently of memory, including partial writes."""
    with database.get_db_conn() as conn:
        _initialize_results(conn)
        conn.execute('''INSERT OR IGNORE INTO memory_review_results
            VALUES (?, ?, ?, ?, ?, ?, ?)''', (operation_id, config_id, result.status,
            result.summary, json.dumps(result.rule_receipts, ensure_ascii=False), result.error, time.time()))
        conn.commit()


@contextmanager
def memory_review_lease(config_id: str):
    owner = str(uuid4())
    now = time.time()
    with database.get_db_conn() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS memory_review_leases (
            config_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)''')
        changed = conn.execute('''INSERT INTO memory_review_leases VALUES (?, ?, ?)
            ON CONFLICT(config_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
            WHERE memory_review_leases.expires_at < ?''', (config_id, owner, now + 3600, now)).rowcount
        conn.commit()
    stop = Event()
    def renew():
        while not stop.wait(30):
            try:
                with database.get_db_conn() as conn:
                    conn.execute('UPDATE memory_review_leases SET expires_at=? WHERE config_id=? AND owner=?',
                                 (time.time() + 3600, config_id, owner))
                    conn.commit()
            except Exception as exc:
                logger.warning('Cannot renew review lease: %s', exc)
    token = _active_lease.set((config_id, owner)) if changed else None
    worker = Thread(target=renew, name='memory-review-lease', daemon=True) if changed else None
    if worker:
        worker.start()
    try:
        yield bool(changed)
    finally:
        stop.set()
        if worker:
            worker.join(timeout=1)
        if token is not None:
            _active_lease.reset(token)
        if changed:
            with database.get_db_conn() as conn:
                conn.execute('DELETE FROM memory_review_leases WHERE config_id=? AND owner=?', (config_id, owner))
                conn.commit()


def serialized_memory_review(function):
    @wraps(function)
    def wrapped(config_id, *args, **kwargs):
        with memory_review_lease(config_id) as acquired:
            if not acquired:
                return False
            return function(config_id, *args, **kwargs)
    return wrapped
