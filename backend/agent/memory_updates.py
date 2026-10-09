"""Durable per-run memory updates; retries never invoke the trading graph."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time
import json

from backend import database
from backend.agent.memory_workflow import memory_review_lease
from backend.utils.logger import setup_logger

logger = setup_logger('RunMemory')
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='run-memory')
_guard = threading.Lock()
_active = {}


def _initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS memory_update_jobs (
        config_id TEXT NOT NULL, run_id TEXT NOT NULL, summary_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(config_id,run_id))''')


def enqueue_memory_update(config_id, run_id, summary_id):
    if not summary_id:
        raise ValueError('Memory update requires a persisted summary ID')
    with database.get_db_conn() as conn:
        _initialize(conn)
        conn.execute('INSERT OR IGNORE INTO memory_update_jobs(config_id,run_id,summary_id) VALUES (?,?,?)',
                     (config_id, run_id, summary_id))
        conn.commit()


def process_memory_update(config_id: str, run_id: str, agent_config: dict) -> bool:
    from backend.agent import agent_graph as graph
    with memory_review_lease(config_id) as acquired:
        if not acquired:
            return False
        with database.get_db_conn() as conn:
            _initialize(conn)
            job = conn.execute('SELECT * FROM memory_update_jobs WHERE config_id=? AND run_id=?',
                               (config_id, run_id)).fetchone()
            if not job or job['status'] == 'completed':
                return bool(job)
            row = conn.execute('SELECT id,timestamp FROM summaries WHERE id=? AND config_id=?',
                               (job['summary_id'], config_id)).fetchone()
            if not row:
                return False
            end = datetime.fromisoformat(row['timestamp'])
            beginning = (end - timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S')
            columns = {item[1] for item in conn.execute('PRAGMA table_info(summaries)')}
            report_field = 'report_json' if 'report_json' in columns else 'NULL AS report_json'
            rows = conn.execute(f'''SELECT id,timestamp,strategy_logic,{report_field} FROM summaries
                WHERE config_id=? AND timestamp>=? AND timestamp<=? AND id<=?
                ORDER BY timestamp,id''', (config_id, beginning, row['timestamp'], row['id'])).fetchall()
        try:
            previous = graph.format_short_memory_for_llm(config_id)
            evidence = graph.format_recent_position_history_for_memory(config_id, agent_config)
            entries = []
            for item in rows:
                text = str(item['strategy_logic'] or '')
                if item['report_json']:
                    try:
                        report = json.loads(item['report_json'])
                        receipts = report.get('execution_results') or []
                        if receipts:
                            text += '\n实际工具回执：\n' + json.dumps(receipts, ensure_ascii=False)
                    except (ValueError, TypeError, AttributeError):
                        text += '\n结构化回执无法读取；执行状态未知。'
                entries.append(f"[{item['timestamp']}] {text}")
            source = ('过去4h滚动窗口：' + beginning + ' → ' + row['timestamp']
                      + '\n当前短期记忆：\n' + previous + '\n本轮及前4h摘要（只出现一次）：\n'
                      + '\n'.join(entries)
                      + graph._memory_evidence_section(evidence))
            result = graph._run_memory_organizer(source, {**agent_config, 'config_id': config_id},
                                                operation_id='run-memory:' + run_id, previous=previous)
            if graph._is_invalid_short_memory_summary(result, source):
                raise ValueError('记忆整理未返回有效结果，旧记忆已保留')
            # Stable subsecond identity prevents same-second manual runs overwriting one another.
            key = end.replace(microsecond=int(row['id']) % 1000000).isoformat(sep=' ', timespec='microseconds')
            graph.save_short_memory(key, row['timestamp'], agent_config.get('symbol', 'Unknown'), config_id,
                                    result, evidence, len(rows), window_start=beginning,
                                    window_end=row['timestamp'], source_summary_ids=[item['id'] for item in rows],
                                    run_id=run_id)
            with database.get_db_conn() as conn:
                conn.execute("UPDATE memory_update_jobs SET status='completed',error='' WHERE config_id=? AND run_id=?",
                             (config_id, run_id))
                conn.commit()
            return True
        except Exception as exc:
            from backend.agent.memory_workflow import get_review_result
            prior = get_review_result('run-memory:' + run_id)
            status = 'partial' if prior and prior.get('status') == 'partial' else 'pending'
            with database.get_db_conn() as conn:
                conn.execute("UPDATE memory_update_jobs SET status=?,attempts=attempts+1,next_attempt=?,error=? WHERE config_id=? AND run_id=?",
                             (status, time.time() + 60, str(exc), config_id, run_id))
                conn.commit()
            logger.warning('Memory update pending for %s: %s', run_id, exc)
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
            rows = conn.execute("SELECT * FROM memory_update_jobs WHERE status='pending' ORDER BY summary_id").fetchall()
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
                _active[cid] = _executor.submit(process_memory_update, cid, row['run_id'], dict(cfg))
