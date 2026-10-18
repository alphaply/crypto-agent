"""Durable accepted triggers. Waiting consumes storage, never worker capacity."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import hashlib
import json

from backend import storage


def scope_hash(config: dict, globals_: dict | None = None) -> str:
    """Only trading scope belongs here; model, strategy and amounts stay live."""
    from backend.utils.spot_portfolio import get_config_symbols
    globals_ = globals_ or {}
    exchange = str(config.get('exchange') or 'binance').lower()
    prefix = 'okx' if exchange == 'okx' else 'binance'
    # Match Config.get_exchange_credentials after profile/provider restoration.
    key = config.get(prefix + '_api_key') or config.get('api_key')
    secret = config.get(prefix + '_secret') or config.get('secret')
    if not key or not secret:
        key = globals_.get('global_' + prefix + '_api_key')
        secret = globals_.get('global_' + prefix + '_secret')
    # A named physical account survives credential rotation. Otherwise an API
    # identity change must not silently move accepted work to another account.
    account = config.get('physical_account_id') or [key, secret]
    identity = [str(config.get('mode') or 'STRATEGY').upper(), exchange,
                config.get('market_type'), config.get('exchange_profile_id'),
                account, sorted(get_config_symbols(config))]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _timestamp() -> str:
    from backend.database import TZ_CN
    return datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')


def _snapshot(conn):
    from backend.config_store import load_runtime_snapshot
    return load_runtime_snapshot(connection=conn, raise_on_error=True) or {}


def enqueue(config_id: str, trigger_key: str, trigger_type: str, scheduled_at: str,
            run_id: str, *, payload: dict | None = None, connection=None) -> bool:
    """Accept once, atomically with optional caller-owned news bookkeeping."""
    with nullcontext(connection) if connection is not None else storage.get_connection() as conn:
        storage.lock_scope(conn, 'runtime-config')
        snapshot = _snapshot(conn)
        config = next((item for item in snapshot.get('agents', [])
                       if item['config_id'] == config_id), None)
        if not config or not config.get('enabled', True) or not snapshot.get('enable_scheduler', True):
            return False
        row = conn.execute('''INSERT INTO agent_run_queue
            (config_id,trigger_key,trigger_type,scheduled_at,run_id,scope_hash,payload)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(config_id,trigger_key) DO NOTHING RETURNING id''',
            (config_id, trigger_key, trigger_type, scheduled_at, run_id,
             scope_hash(config, snapshot), json.dumps(payload or {}))).fetchone()
        if not row:
            return False
        run = conn.execute('''INSERT INTO scheduler_runs
            (config_id,job_type,scheduled_at,status,created_at,updated_at,trigger_key,phase,progress_message)
            VALUES(%s,'agent',%s,'QUEUED',%s,%s,%s,'queued','等待运行名额，已保存到队列') RETURNING id''',
            (config_id, scheduled_at, _timestamp(), _timestamp(), trigger_key)).fetchone()
        conn.execute('UPDATE agent_run_queue SET scheduler_run_id=%s WHERE id=%s', (run[0], row[0]))
        if connection is None:
            conn.commit()
        return True


def cancel_unstarted(conn, config_id: str | None, reason: str) -> int:
    where, args = (' AND config_id=%s', [config_id]) if config_id is not None else ('', [])
    rows = conn.execute("UPDATE agent_run_queue SET status='CANCELLED',reason=%s,finished_at=clock_timestamp() "
                        "WHERE status IN ('QUEUED','CLAIMED')" + where + ' RETURNING scheduler_run_id',
                        [reason, *args]).fetchall()
    ids = [row[0] for row in rows if row[0] is not None]
    if ids:
        conn.execute("UPDATE scheduler_runs SET status='CANCELLED',phase='cancelled',error=%s,"
                     'progress_message=%s,finished_at=%s,updated_at=%s WHERE id=ANY(%s)',
                     (reason, reason, _timestamp(), _timestamp(), ids))
    return len(rows)


def synchronize_configuration(conn, previous: dict | None, current: dict) -> None:
    """Called inside the runtime-config transaction before any history purge."""
    old = {item['config_id']: item for item in (previous or {}).get('agents', [])}
    new = {item['config_id']: item for item in current.get('agents', [])}
    if not current.get('enable_scheduler', True):
        cancel_unstarted(conn, None, 'scheduler_paused')
        return
    for config_id, before in old.items():
        after = new.get(config_id)
        if not after:
            cancel_unstarted(conn, config_id, 'configuration_deleted')
        elif not after.get('enabled', True):
            cancel_unstarted(conn, config_id, 'configuration_paused')
        elif scope_hash(before, previous) != scope_hash(after, current):
            cancel_unstarted(conn, config_id, 'trading_scope_changed')


def claim_next(owner: str) -> dict | None:
    """Choose the oldest runnable task; an active A never prevents a ready B."""
    with storage.get_connection() as conn:
        storage.lock_scope(conn, 'agent-queue-dispatch')
        row = conn.execute('''SELECT q.* FROM agent_run_queue q
            WHERE q.status='QUEUED' AND NOT EXISTS (
                SELECT 1 FROM agent_run_queue active WHERE active.config_id=q.config_id
                AND active.status IN ('CLAIMED','RUNNING'))
            ORDER BY q.id LIMIT 1 FOR UPDATE OF q''').fetchone()
        if not row:
            return None
        conn.execute("UPDATE agent_run_queue SET status='CLAIMED',worker_owner=%s WHERE id=%s", (owner, row['id']))
        conn.commit()
        return {**storage.record(row), 'status': 'CLAIMED', 'worker_owner': owner}


def release_claim(queue_id: int, owner: str) -> None:
    with storage.get_connection() as conn:
        conn.execute("UPDATE agent_run_queue SET status='QUEUED',worker_owner=NULL "
                     "WHERE id=%s AND worker_owner=%s AND status='CLAIMED'", (queue_id, owner))
        conn.commit()


def start_claimed(queue_id: int, owner: str) -> tuple[dict, dict] | None:
    """Recheck authorization and capture fresh configuration immediately before work."""
    with storage.get_connection() as conn:
        storage.lock_scope(conn, 'runtime-config')
        row = conn.execute("SELECT * FROM agent_run_queue WHERE id=%s AND worker_owner=%s "
                           "AND status='CLAIMED' FOR UPDATE", (queue_id, owner)).fetchone()
        if not row:
            return None
        snapshot = _snapshot(conn)
        config = next((item for item in snapshot.get('agents', []) if item['config_id'] == row['config_id']), None)
        reason = ('configuration_deleted' if not config else
                  'configuration_paused' if not config.get('enabled', True) else
                  'scheduler_paused' if not snapshot.get('enable_scheduler', True) else
                  'trading_scope_changed' if scope_hash(config, snapshot) != row['scope_hash'] else None)
        if reason:
            cancel_unstarted(conn, row['config_id'], reason)
            conn.commit()
            return None
        config = dict(config)
        payload = row['payload']
        if row['trigger_type'] == 'news':
            from backend.app.services.news_triggers import model_override
            provider_id = payload.get('provider_id')
            # Use the current selected news model, keeping the accepted articles.
            for rule in snapshot.get('news', {}).get('trigger_rules', []):
                if rule.get('config_id') == row['config_id']:
                    provider_id = rule.get('provider_id') or provider_id
                    break
            provider = next((p for p in snapshot.get('llm_providers', []) if p['provider_id'] == provider_id), None)
            if not provider:
                _finish(conn, row, 'FAILED', 'news_model_unavailable')
                conn.commit()
                return None
            config = model_override(config, provider)
            config['_news_event'] = payload['event']
        conn.execute("UPDATE agent_run_queue SET status='RUNNING',started_at=clock_timestamp() WHERE id=%s", (queue_id,))
        conn.execute("UPDATE scheduler_runs SET status='RUNNING',started_at=%s,updated_at=%s WHERE id=%s",
                     (_timestamp(), _timestamp(), row['scheduler_run_id']))
        conn.commit()
        return config, storage.record(row, status='RUNNING')


def _finish(conn, row, status: str, reason: str | None):
    conn.execute('UPDATE agent_run_queue SET status=%s,reason=%s,finished_at=clock_timestamp() WHERE id=%s',
                 (status, reason, row['id']))
    conn.execute('UPDATE scheduler_runs SET status=%s,error=%s,finished_at=%s,updated_at=%s WHERE id=%s',
                 (status, reason, _timestamp(), _timestamp(), row['scheduler_run_id']))


def finish(queue_id: int, owner: str, status: str, reason: str | None = None) -> None:
    if status not in {'FINISHED', 'FAILED'}:
        raise ValueError('Invalid completed queue status')
    with storage.get_connection() as conn:
        row = conn.execute("SELECT id,scheduler_run_id FROM agent_run_queue WHERE id=%s AND worker_owner=%s "
                           "AND status='RUNNING' FOR UPDATE", (queue_id, owner)).fetchone()
        if row:
            _finish(conn, row, status, reason)
        conn.commit()


def recover_after_restart() -> None:
    """Only call while holding the process-lifetime scheduler file lock."""
    with storage.get_connection() as conn:
        conn.execute("UPDATE agent_run_queue SET status='QUEUED',worker_owner=NULL WHERE status='CLAIMED'")
        rows = conn.execute("UPDATE agent_run_queue SET status='INTERRUPTED',finished_at=clock_timestamp(),"
                            "reason='worker_interrupted_no_replay' WHERE status='RUNNING' RETURNING scheduler_run_id").fetchall()
        ids = [row[0] for row in rows if row[0] is not None]
        if ids:
            conn.execute("UPDATE scheduler_runs SET status='INTERRUPTED',phase='interrupted',"
                         "error='worker_interrupted_no_replay',finished_at=%s,updated_at=%s WHERE id=ANY(%s)",
                         (_timestamp(), _timestamp(), ids))
        # Pre-queue releases did not persist a claim/start boundary. Never replay
        # their unfinished rounds just because they have no final summary.
        conn.execute("UPDATE scheduler_runs s SET status='INTERRUPTED',phase='interrupted',"
                     "error='worker_interrupted_no_replay',finished_at=%s,updated_at=%s "
                     "WHERE s.job_type='agent' AND s.status IN ('QUEUED','RUNNING') "
                     "AND NOT EXISTS (SELECT 1 FROM agent_run_queue q WHERE q.scheduler_run_id=s.id)",
                     (_timestamp(), _timestamp()))
        conn.commit()


def _public_queue(row) -> dict | None:
    if row is None:
        return None
    return {key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in storage.record(row).items()
            if key in {'id','config_id','trigger_type','run_id','scheduled_at','status','enqueued_at','reason'}}


def get_runtime_states(config_ids: list[str], *, connection=None) -> dict:
    result = {cid: {'running': None, 'queued_count': 0, 'next_queued': None, 'last_run': None} for cid in config_ids}
    if not result:
        return result
    with nullcontext(connection) if connection is not None else storage.get_connection() as conn:
        for row in conn.execute("SELECT config_id,COUNT(*) FROM agent_run_queue WHERE config_id=ANY(%s) "
                                "AND status IN ('QUEUED','CLAIMED') GROUP BY config_id", (config_ids,)):
            result[row[0]]['queued_count'] = row[1]
        for row in conn.execute("SELECT DISTINCT ON (config_id) * FROM agent_run_queue WHERE config_id=ANY(%s) "
                                "AND status IN ('QUEUED','CLAIMED') ORDER BY config_id,id", (config_ids,)):
            result[row['config_id']]['next_queued'] = _public_queue(row)
        for row in conn.execute("SELECT DISTINCT ON (config_id) * FROM scheduler_runs WHERE config_id=ANY(%s) "
                                "AND job_type='agent' AND status='RUNNING' ORDER BY config_id,id DESC", (config_ids,)):
            result[row['config_id']]['running'] = storage.record(row)
        for row in conn.execute("SELECT DISTINCT ON (config_id) * FROM scheduler_runs WHERE config_id=ANY(%s) "
                                "AND job_type='agent' AND status NOT IN ('RUNNING','QUEUED') ORDER BY config_id,id DESC", (config_ids,)):
            result[row['config_id']]['last_run'] = storage.record(row)
    return result


def get_runtime_state(config_id: str, *, connection=None) -> dict:
    return get_runtime_states([config_id], connection=connection)[config_id]
