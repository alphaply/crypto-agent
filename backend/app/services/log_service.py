"""Private, bounded log browsing and maintenance of existing retention limits."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.utils.logger import LOG_BACKUPS, LOG_MAX_BYTES, log_file_lock, log_path, setup_logger
from backend.utils.log_safety import exception_diagnostic, redact_log_text, register_snapshot_secrets

logger = setup_logger('LogMaintenance')
SCAN_BYTES = 4 * 1024 * 1024
ENTRY_BYTES = 16000
_HEADER = re.compile(rb'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] \[([^\]\r\n]+)\] ', re.M)
_LEVELS = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}
_tick_lock = threading.Lock()
_next_check = 0.0
_worker: threading.Thread | None = None


class LogCursorExpired(ValueError):
    pass


def _files(path: Path) -> list[Path]:
    return [p for p in [path, *(Path(str(path) + f'.{i}') for i in range(1, LOG_BACKUPS + 1))]
            if p.is_file() and not p.is_symlink()]


def _identity(path: Path) -> str:
    stat = path.stat()
    return f'{stat.st_dev}:{stat.st_ino}'


def _cursor(path: Path, position: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([_identity(path), position]).encode()).decode()


def read_logs(*, limit: int = 100, min_level: str = 'INFO', query: str = '', cursor: str | None = None) -> dict:
    if min_level not in _LEVELS or not 1 <= limit <= 500 or len(query) > 200:
        raise ValueError('Invalid log filter')
    # Register current credentials so legacy lines receive the same redaction as new writes.
    try:
        from backend.config_store import load_effective_runtime_snapshot
        register_snapshot_secrets(load_effective_runtime_snapshot())
    except Exception:
        pass
    path = log_path()
    entries, budget, next_cursor = [], SCAN_BYTES, None
    with log_file_lock(path):
        files = _files(path)
        start, position = 0, None
        if cursor:
            try:
                identity, position = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if type(position) is not int or position < 0 or not isinstance(identity, str):
                    raise ValueError
                start = next(i for i, p in enumerate(files) if _identity(p) == identity)
                if position > files[start].stat().st_size:
                    raise ValueError
            except (ValueError, TypeError, StopIteration, json.JSONDecodeError):
                raise LogCursorExpired('日志已轮换或游标已失效，请刷新后查看。') from None
        for index in range(start, len(files)):
            file = files[index]
            end = position if index == start and position is not None else file.stat().st_size
            begin = max(0, end - budget)
            with file.open('rb') as stream:
                stream.seek(begin)
                raw = stream.read(end - begin)
            budget -= len(raw)
            headers = list(_HEADER.finditer(raw))
            for offset in range(len(headers) - 1, -1, -1):
                header = headers[offset]
                boundary = headers[offset + 1].start() if offset + 1 < len(headers) else len(raw)
                body = redact_log_text(raw[header.end():boundary].decode('utf-8', 'replace').rstrip())
                level = header[2].decode()
                module = header[3].decode('utf-8', 'replace')
                next_cursor = _cursor(file, begin + header.start())
                if _LEVELS[level] < _LEVELS[min_level] or query.lower() not in (module + '\n' + body).lower():
                    continue
                entries.append({'id': f'{_identity(file)}:{begin + header.start()}',
                                'timestamp': header[1].decode(), 'level': level, 'module': module,
                                'message': body[:ENTRY_BYTES], 'truncated': len(body) > ENTRY_BYTES,
                                'file': file.name})
                if len(entries) == limit:
                    return {'entries': entries, 'next_cursor': next_cursor, 'scan_limited': False}
            if begin:
                next_cursor = _cursor(file, begin + headers[0].start() if headers else begin)
                return {'entries': entries, 'next_cursor': next_cursor, 'scan_limited': True}
            if index + 1 < len(files):
                next_cursor = _cursor(files[index + 1], files[index + 1].stat().st_size)
                if budget <= 0:
                    return {'entries': entries, 'next_cursor': next_cursor, 'scan_limited': True}
    return {'entries': entries, 'next_cursor': None, 'scan_limited': False}


def log_status() -> dict:
    from backend.database.agent_runs import RETENTION_DAYS, MAX_RUNS_PER_CONFIG, MAX_NEWS_RUNS
    path = log_path()
    with log_file_lock(path):
        files = [{'name': p.name, 'bytes': p.stat().st_size,
                  'modified_at': datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
                 for p in _files(path)]
    try:
        from backend.database import news
        maintenance = news.read_state('log-maintenance')
    except Exception as exc:
        maintenance = {'status': 'unavailable', 'error': redact_log_text(exc)[:2000]}
    return {'files': files, 'total_bytes': sum(f['bytes'] for f in files),
            'policy': {'max_file_bytes': LOG_MAX_BYTES, 'backups': LOG_BACKUPS,
                       'agent_run_days': RETENTION_DAYS, 'agent_runs_per_config': MAX_RUNS_PER_CONFIG,
                       'news_model_calls': MAX_NEWS_RUNS, 'news_processing_runs': 20,
                       'cleanup_interval_seconds': 3600}, 'maintenance': maintenance}


def maintain_logs() -> bool:
    from backend.database import get_db_conn, news
    from backend.database.agent_runs import RETENTION_DAYS, MAX_RUNS_PER_CONFIG, MAX_NEWS_RUNS
    now = datetime.now(timezone.utc)
    owner = news.acquire_lease('log-maintenance', seconds=300, due_before=(now - timedelta(hours=1)).isoformat())
    if not owner:
        return False
    news.update_state('log-maintenance', {'status': 'running', 'last_attempt_at': now.isoformat(), 'error': None}, owner=owner)
    deleted = {'agent_runs': 0, 'news_processing_runs': 0, 'log_backups': 0}
    removed_bytes, stage = 0, 'database'
    try:
        with get_db_conn() as conn:
            expired = conn.execute('DELETE FROM agent_runs WHERE started_at < %s',
                                   ((now - timedelta(days=RETENTION_DAYS)).isoformat(),)).rowcount
            excess = conn.execute('''DELETE FROM agent_runs WHERE run_id IN (
                SELECT run_id FROM (SELECT run_id, config_id, ROW_NUMBER() OVER
                (PARTITION BY config_id ORDER BY started_at DESC NULLS LAST, storage_id DESC) AS rank
                FROM agent_runs) ranked WHERE rank > CASE WHEN config_id='news-intelligence' THEN %s ELSE %s END
            )''', (MAX_NEWS_RUNS, MAX_RUNS_PER_CONFIG)).rowcount
            processing = conn.execute('''DELETE FROM news_processing_runs WHERE run_id NOT IN
                (SELECT run_id FROM news_processing_runs ORDER BY started_at DESC NULLS LAST LIMIT 20)''').rowcount
            conn.commit()
        deleted.update(agent_runs=expired + excess, news_processing_runs=processing)
        stage = 'log_files'
        path = log_path()
        with log_file_lock(path):
            for file in path.parent.glob('app.log.*'):
                suffix = file.name.removeprefix('app.log.')
                if suffix.isascii() and suffix.isdecimal() and int(suffix) > LOG_BACKUPS and file.is_file() and not file.is_symlink():
                    size = file.stat().st_size
                    file.unlink()
                    deleted['log_backups'] += 1
                    removed_bytes += size
        result = {'status': 'success', 'last_success_at': datetime.now(timezone.utc).isoformat(),
                  'error': None, 'deleted': deleted,
                  'removed_bytes': removed_bytes}
        news.update_state('log-maintenance', result, owner=owner, release=True)
        logger.info('Log cleanup completed: model_calls=%s processing_runs=%s backups=%s bytes=%s',
                    expired + excess, processing, deleted['log_backups'], removed_bytes)
        return True
    except Exception as exc:
        diagnostic = exception_diagnostic(exc, stage=stage)
        logger.error('Log cleanup failed: %s\n%s', diagnostic['message'], diagnostic['traceback'])
        news.update_state('log-maintenance', {'status': 'error', 'error': diagnostic['message'],
                                            'error_details': diagnostic, 'deleted': deleted,
                                            'removed_bytes': removed_bytes}, owner=owner, release=True)
        return False


def tick_log_cleanup() -> None:
    global _next_check, _worker
    with _tick_lock:
        if time.monotonic() < _next_check or (_worker and _worker.is_alive()):
            return
        _next_check = time.monotonic() + 60
        def work():
            try:
                maintain_logs()
            except Exception as exc:
                diagnostic = exception_diagnostic(exc, stage='log_cleanup')
                logger.error('Cannot start log cleanup: %s\n%s', diagnostic['message'], diagnostic['traceback'])
        _worker = threading.Thread(target=work, name='log-cleanup', daemon=True)
        _worker.start()


def start_log_maintenance():
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            tick_log_cleanup()
            stop.wait(60)
    thread = threading.Thread(target=loop, name='log-maintenance', daemon=True)
    thread.start()
    return stop
