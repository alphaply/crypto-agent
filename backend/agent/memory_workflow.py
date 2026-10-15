"""Serialize expensive reviews across scheduler/manual requests and app workers."""
from backend.storage import record as storage_record
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
_review_projection = ContextVar('memory_projection_metadata', default=None)


def verify_memory_lease() -> None:
    lease = _active_lease.get()
    if lease is None:
        return
    with database.get_db_conn() as conn:
        valid = conn.execute('SELECT 1 FROM memory_review_leases WHERE config_id=%s AND owner=%s AND expires_at>%s',
                             (*lease, time.time())).fetchone()
    if not valid:
        raise RuntimeError('记忆整理租约已失效，停止修改与保存')


def _initialize_results(conn):
    """Schema is installed by the explicit PostgreSQL migration command."""
    return None


def get_review_result(operation_id: str) -> dict | None:
    with database.get_db_conn() as conn:
        _initialize_results(conn)
        row = conn.execute('SELECT * FROM memory_review_results WHERE operation_id=%s', (operation_id,)).fetchone()
        if not row:
            return None
        result = storage_record(row)
        result['rule_receipts'] = json.loads(result['rule_receipts'])
        result['projection_metadata'] = json.loads(result['projection_metadata'])
        return result


def save_review_result(config_id: str, operation_id: str, result) -> None:
    """Persist terminal outcomes independently of memory, including partial writes."""
    verify_memory_lease()
    with database.get_db_conn() as conn:
        _initialize_results(conn)
        conn.execute('INSERT INTO memory_review_results\n            (operation_id,config_id,status,summary,rule_receipts,error,created_at,projection_metadata)\n            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING', (operation_id, config_id, result.status,
            result.summary, json.dumps(result.rule_receipts, ensure_ascii=False), result.error, time.time(),
            json.dumps(getattr(result, 'projection_metadata', {}), ensure_ascii=False)))
        conn.commit()


@contextmanager
def memory_review_lease(config_id: str):
    owner = str(uuid4())
    now = time.time()
    with database.get_db_conn() as conn:
        # Table installed by the explicit PostgreSQL migration.
        changed = conn.execute('INSERT INTO memory_review_leases(config_id,owner,expires_at) VALUES (%s, %s, %s)\n            ON CONFLICT(config_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at\n            WHERE memory_review_leases.expires_at < %s', (config_id, owner, now + 3600, now)).rowcount
        conn.commit()
    stop = Event()
    def renew():
        while not stop.wait(30):
            try:
                with database.get_db_conn() as conn:
                    conn.execute('UPDATE memory_review_leases SET expires_at=%s WHERE config_id=%s AND owner=%s',
                                 (time.time() + 3600, config_id, owner))
                    conn.commit()
            except Exception as exc:
                logger.warning('Cannot renew review lease: %s', exc)
    token = _active_lease.set((config_id, owner)) if changed else None
    projection_token = _review_projection.set(None) if changed else None
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
        if projection_token is not None:
            _review_projection.reset(projection_token)
        if changed:
            with database.get_db_conn() as conn:
                conn.execute('DELETE FROM memory_review_leases WHERE config_id=%s AND owner=%s', (config_id, owner))
                conn.commit()


def serialized_memory_review(function):
    @wraps(function)
    def wrapped(config_id, *args, **kwargs):
        with memory_review_lease(config_id) as acquired:
            if not acquired:
                return False
            return function(config_id, *args, **kwargs)
    return wrapped
