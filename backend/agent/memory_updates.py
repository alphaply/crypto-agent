"""Durable per-run memory updates; retries never invoke the trading graph."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import threading
import time

from backend import database
from backend.agent.memory_workflow import memory_review_lease
from backend.utils.logger import setup_logger
from backend.utils.runtime_capacity import CapacityBusy, submit_capacity, positive_setting

logger = setup_logger('RunMemory')
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='run-memory')
_guard = threading.Lock()
_active = {}
MAX_JOB_ATTEMPTS = 3


def _initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_update_jobs (
        config_id TEXT NOT NULL, run_id TEXT NOT NULL, summary_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(config_id,run_id))''')
    from backend.agent.memory_contract_recovery import initialize_contract_state
    initialize_contract_state(conn, 'memory_update_jobs')


def _enqueue(conn, config_id, run_id, summary_id):
    from backend.agent.memory_contract_recovery import CONTRACT_REVISION, job_source
    from backend.utils.trade_evidence import configured_account_scope
    if not summary_id:
        raise ValueError('Memory update requires a persisted summary ID')
    _initialize(conn)
    row = conn.execute('SELECT symbol FROM summaries WHERE config_id=? AND id=?',
                       (config_id, summary_id)).fetchone()
    if not row:
        raise ValueError('Memory source summary no longer exists')
    witness = job_source(conn, config_id, summary_id, configured_account_scope(config_id), row['symbol'])
    if not witness:
        raise ValueError('Memory source summary has no valid symbol')
    conn.execute('INSERT OR IGNORE INTO memory_update_jobs'
                 '(config_id,run_id,summary_id,review_contract_revision,review_source) VALUES (?,?,?,?,?)',
                 (config_id, run_id, summary_id, CONTRACT_REVISION, json.dumps(witness)))


def enqueue_memory_update(config_id, run_id, summary_id):
    with database.get_db_conn() as conn:
        _enqueue(conn, config_id, run_id, summary_id)
        conn.commit()


def process_memory_update(config_id: str, run_id: str, agent_config: dict) -> bool:
    from backend.agent import memory_service as memory
    with memory_review_lease(config_id) as acquired:
        if not acquired:
            return False
        with database.get_db_conn() as conn:
            _initialize(conn)
            job = conn.execute('SELECT * FROM memory_update_jobs WHERE config_id=? AND run_id=?',
                               (config_id, run_id)).fetchone()
            if not job or job['status'] != 'pending':
                return bool(job and job['status'] == 'completed')
            if job['next_attempt'] > time.time():
                return False
        try:
            from backend.agent.memory_contract_recovery import CONTRACT_REVISION, job_source
            from backend.utils.trade_evidence import configured_account_scope
            # Every new job retains a source/account witness across restarts.
            # A mismatch fails before model work and is checked again at save.
            captured_source = json.loads(job['review_source'])
            with database.get_db_conn() as conn:
                current_source = job_source(conn, config_id, job['summary_id'],
                                            configured_account_scope(config_id), agent_config.get('symbol', ''))
                if not current_source or (captured_source and captured_source != current_source):
                    raise ValueError('Memory recovery source or account changed')
                captured_source = captured_source or current_source
                conn.execute('UPDATE memory_update_jobs SET review_contract_revision=?,review_source=? '
                             'WHERE config_id=? AND run_id=?',
                             (CONTRACT_REVISION, json.dumps(captured_source), config_id, run_id))
                conn.commit()
                row = conn.execute('SELECT id,timestamp,strategy_logic FROM summaries WHERE id=? AND config_id=?',
                                   (job['summary_id'], config_id)).fetchone()
                if not row:
                    raise ValueError('Memory source summary no longer exists')
                from backend.summary_memory_source import valid_compressed_summary
                if not valid_compressed_summary(row['strategy_logic']):
                    raise ValueError('Memory source strategy compression did not succeed')
                end = datetime.fromisoformat(row['timestamp'])
                beginning = (end - timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S')
                columns = {item[1] for item in conn.execute('PRAGMA table_info(summaries)')}
                report_field = 'report_json' if 'report_json' in columns else 'NULL AS report_json'
                decision_field = 'decision_json' if 'decision_json' in columns else 'NULL AS decision_json'
                rows = conn.execute(f'''SELECT id,config_id,symbol,timestamp,strategy_logic,{report_field},{decision_field} FROM summaries
                    WHERE config_id=? AND timestamp>=? AND timestamp<=? AND id<=?
                    ORDER BY timestamp,id''', (config_id, beginning, row['timestamp'], row['id'])).fetchall()
            from backend.agent.context_projections import capture_sources, memory_summary_rows
            rows = memory_summary_rows(config_id, [dict(item) for item in rows])
            if not any(item['id'] == job['summary_id'] for item in rows):
                raise ValueError('Memory source summary is outside the current account or symbol')
            captured = capture_sources(config_id, summary_rows=rows)
            previous = memory.format_short_memory_for_llm(config_id)
            evidence = memory.format_recent_position_history_for_memory(config_id, agent_config)
            source = memory.build_memory_input(rows, previous, evidence, beginning, row['timestamp'])
            result = memory.organize_memory(source, {**agent_config, 'config_id': config_id},
                                            operation_id='run-memory:' + run_id, source_metadata=captured)
            if memory.is_invalid_memory(result, source):
                raise ValueError('记忆整理未返回有效结果，旧记忆已保留')
            if captured_source:
                with database.get_db_conn() as conn:
                    if captured_source != job_source(conn, config_id, job['summary_id'],
                                                      configured_account_scope(config_id), agent_config.get('symbol', '')):
                        raise ValueError('Memory recovery source or account changed')
            # Stable subsecond identity prevents same-second manual runs overwriting one another.
            key = end.replace(microsecond=int(row['id']) % 1000000).isoformat(sep=' ', timespec='microseconds')
            memory.save_short_memory(key, row['timestamp'], agent_config.get('symbol', 'Unknown'), config_id,
                                    result, evidence, len(rows), window_start=beginning,
                                    window_end=row['timestamp'], source_summary_ids=[item['id'] for item in rows],
                                    run_id=run_id, **({'source_guard': captured_source} if captured_source else {}))
            with database.get_db_conn() as conn:
                conn.execute("UPDATE memory_update_jobs SET status='completed',error='' WHERE config_id=? AND run_id=?",
                             (config_id, run_id))
                conn.commit()
            return True
        except Exception as exc:
            from backend.agent.memory_workflow import get_review_result
            prior = get_review_result('run-memory:' + run_id)
            attempts = job['attempts'] + 1
            if prior and prior.get('status') == 'partial':
                status = 'partial'
            else:
                status = 'failed' if attempts >= MAX_JOB_ATTEMPTS else 'pending'
            with database.get_db_conn() as conn:
                conn.execute("UPDATE memory_update_jobs SET status=?,attempts=attempts+1,next_attempt=?,error=? WHERE config_id=? AND run_id=?",
                             (status, time.time() + 60 * 5 ** min(attempts - 1, 2), str(exc), config_id, run_id))
                conn.commit()
            logger.warning('Memory update %s for %s (attempt %s/%s): %s', status, run_id, attempts, MAX_JOB_ATTEMPTS, exc)
            return False


def tick_memory_updates():
    from backend.config import config
    with _guard:
        for config_id, future in list(_active.items()):
            if future.done():
                try:
                    future.result()
                except Exception as exc:
                    logger.warning('Memory worker failed: %s', exc)
                _active.pop(config_id, None)
        with database.get_db_conn() as conn:
            _initialize(conn)
            active_ids = list(_active)
            exclusion = ' AND config_id NOT IN (' + ','.join('?' for _ in active_ids) + ')' if active_ids else ''
            rows = conn.execute("SELECT j.* FROM memory_update_jobs j WHERE j.status='pending'" + exclusion +
                " AND NOT EXISTS (SELECT 1 FROM memory_update_jobs older WHERE older.config_id=j.config_id "
                "AND older.status='pending' AND older.summary_id<j.summary_id) ORDER BY j.summary_id LIMIT ?",
                (*active_ids, positive_setting('RUNTIME_SCAN_BATCH_SIZE', 32))).fetchall()
        selected = set()
        for row in rows:
            cid = row['config_id']
            if cid in selected:
                continue
            selected.add(cid)  # Keep each configuration's memories strictly in run order.
            if cid in _active or row['next_attempt'] > time.time():
                continue
            cfg = config.get_config_by_id(cid)
            if cfg:
                try:
                    _active[cid] = submit_capacity(_executor, 'memory', process_memory_update, cid, row['run_id'], dict(cfg))
                except CapacityBusy:
                    break
