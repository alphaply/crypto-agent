"""Read-only database diagnostics and explicitly previewed retention maintenance."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from backend import database

# Trading evidence, credentials, configurations and current plans are never purge targets.
CLEANABLE = {
    'news_snapshots': 'timestamp',
    'summaries': 'timestamp',
    'mock_balance_history': 'timestamp',
    'balance_history': 'timestamp',
    'token_usage': 'timestamp',
    'scheduler_runs': 'created_at',
    'short_memories': 'bucket_start',
}
_maintenance_lock = threading.Lock()


def inspect_database(path: str | Path | None = None) -> dict:
    target = Path(path or database.DB_NAME).resolve()
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        free = conn.execute('PRAGMA freelist_count').fetchone()[0]
        sizes = {}
        try:
            sizes = dict(conn.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name'))
        except sqlite3.OperationalError:
            pass
        rows = []
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {r[1]: r[2] for r in conn.execute(f'PRAGMA table_info({quoted})')}
            time_col = next((c for c in [CLEANABLE.get(name), 'timestamp_ms', 'timestamp', 'closed_at_ms', 'created_at'] if c in columns), None)
            span = conn.execute(f'SELECT MIN("{time_col}"),MAX("{time_col}") FROM {quoted}').fetchone() if time_col else (None, None)
            rows.append({'table': name, 'rows': conn.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0],
                         'bytes': sizes.get(name), 'oldest': span[0], 'newest': span[1],
                         'cleanable': name in CLEANABLE and CLEANABLE[name] in columns})
        unlinked = None
        if {'execution_fills', 'execution_order_links'} <= set(names):
            unlinked = conn.execute('SELECT COUNT(*) FROM execution_fills f LEFT JOIN execution_order_links l '
                                    'USING(account_scope,symbol,order_id) WHERE l.order_id IS NULL').fetchone()[0]
        return {'database_path': str(target) if path is None else '导入副本（只读临时文件）',
                'integrity': integrity, 'file_bytes': target.stat().st_size,
                'allocated_bytes': pages * page_size, 'reclaimable_bytes': free * page_size,
                'wal_bytes': Path(str(target) + '-wal').stat().st_size if Path(str(target) + '-wal').exists() else 0,
                'unlinked_fills': unlinked, 'tables': rows,
                'size_note': '表占用不可用时显示未知；空闲页需执行空间回收才会缩小文件。'}


def _selection(conn, tables: list[str], before: str, config_id: str | None):
    cutoff = datetime.fromisoformat(before)
    if cutoff.tzinfo is not None:
        raise ValueError('截止时间使用北京时间，不含时区后缀')
    if cutoff > datetime.now(database.TZ_CN).replace(tzinfo=None) - timedelta(days=7):
        raise ValueError('至少保留最近 7 天数据')
    if not tables or not set(tables) <= CLEANABLE.keys():
        raise ValueError('请选择允许清理的数据类别；成交与保护证据不可清理')
    selected = {}
    digest = hashlib.sha256(json.dumps([sorted(set(tables)), before, config_id]).encode())
    for table in sorted(set(tables)):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if CLEANABLE[table] not in cols:
            raise ValueError(f'{table} 不支持此版本清理')
        predicate = f'"{CLEANABLE[table]}" < ?'
        args = [cutoff.strftime('%Y-%m-%d %H:%M:%S')]
        if config_id:
            if 'config_id' not in cols:
                raise ValueError(f'{table} 是全局数据，不能按任务清理')
            predicate += ' AND config_id=?'
            args.append(config_id)
        if table == 'short_memories':
            predicate += ' AND rowid NOT IN (SELECT MAX(rowid) FROM short_memories GROUP BY config_id)'
        ids = [r[0] for r in conn.execute(f'SELECT rowid FROM "{table}" WHERE {predicate} ORDER BY rowid', args)]
        digest.update(json.dumps([table, ids]).encode())
        selected[table] = {'count': len(ids), 'predicate': predicate, 'args': args}
    return selected, digest.hexdigest()


def preview_cleanup(tables: list[str], before: str, config_id: str | None = None) -> dict:
    with database.get_db_conn() as conn:
        selected, token = _selection(conn, tables, before, config_id)
    return {'counts': {t: s['count'] for t, s in selected.items()}, 'preview_token': token,
            'notice': '将先备份再删除；历史分析和统计可能减少，成交账本与活跃保护保留。'}


def _backup() -> str:
    directory = Path(database.DB_NAME).resolve().parent / 'maintenance-backups'
    directory.mkdir(parents=True, exist_ok=True)
    name = f'{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.db'
    with database.get_db_conn() as source, closing(sqlite3.connect(directory / name)) as destination:
        source.backup(destination)
    return name


def clean_database(tables: list[str], before: str, preview_token: str, config_id: str | None = None) -> dict:
    with _maintenance_lock:
        preview = preview_cleanup(tables, before, config_id)
        if preview['preview_token'] != preview_token:
            raise ValueError('数据已变化，请重新预览')
        backup = _backup()
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            selected, token = _selection(conn, tables, before, config_id)
            if token != preview_token:
                raise ValueError('数据已变化，请重新预览')
            deleted = {t: conn.execute(f'DELETE FROM "{t}" WHERE {s["predicate"]}', s['args']).rowcount
                       for t, s in selected.items()}
            conn.execute('CREATE TABLE IF NOT EXISTS database_maintenance_log '
                         '(id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)')
            conn.execute('INSERT INTO database_maintenance_log(timestamp,payload) VALUES(?,?)',
                         (database._current_timestamp(), json.dumps({'deleted': deleted, 'backup': backup})))
            conn.commit()
        return {'deleted': deleted, 'backup': backup}


def compact_database() -> dict:
    with _maintenance_lock:
        before = Path(database.DB_NAME).stat().st_size
        with database.get_db_conn() as conn:
            conn.execute('VACUUM')
            checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint[0]:
                raise sqlite3.OperationalError('WAL checkpoint busy; retry later')
        return {'before_bytes': before, 'after_bytes': Path(database.DB_NAME).stat().st_size}


def rebuild_history() -> dict:
    from backend.utils.execution_ledger import rebuild_execution_position_history
    with _maintenance_lock:
        backup = _backup()
        with database.get_db_conn() as conn:
            targets = conn.execute('SELECT DISTINCT config_id,account_scope,symbol FROM execution_order_links').fetchall()
        results = []
        for row in targets:
            result = rebuild_execution_position_history(row['config_id'], row['account_scope'], row['symbol'])
            results.append({'config_id': row['config_id'], 'symbol': row['symbol'],
                            'completed': len(result['completed']), 'excluded': len(result['excluded_cycles']),
                            'unmatched': result['unmatched_fill_count']})
        return {'backup': backup, 'results': results}
