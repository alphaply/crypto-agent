"""Read-only database diagnostics and explicitly previewed retention maintenance."""
from __future__ import annotations

import hashlib
import json
from backend import storage
from backend.storage_paths import DATA_DIR
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
    if path is not None:
        from .sqlite_analysis import inspect_sqlite_copy
        return inspect_sqlite_copy(path)
    from backend.storage import database_label, table_names, table_columns, sql
    with database.get_db_conn() as conn:
        rows = []
        names = table_names(conn)
        for name in sorted(names):
            columns = {row['name'] for row in table_columns(conn, name)}
            time_col = next((c for c in [CLEANABLE.get(name), 'timestamp_ms', 'timestamp', 'closed_at_ms', 'created_at'] if c in columns), None)
            table = sql.Identifier(name)
            count = conn.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(table)).fetchone()[0]
            size = conn.execute('SELECT pg_total_relation_size(%s::regclass)', (name,)).fetchone()[0]
            span = conn.execute(sql.SQL('SELECT MIN({0}),MAX({0}) FROM {1}').format(sql.Identifier(time_col),table)).fetchone() if time_col else (None,None)
            rows.append({'table':name,'rows':count,'bytes':size,'oldest':span[0],'newest':span[1],
                         'task_scoped':'config_id' in columns,'cleanable':name in CLEANABLE and CLEANABLE[name] in columns})
        unlinked = conn.execute('SELECT COUNT(*) FROM execution_fills f LEFT JOIN execution_order_links l USING(account_scope,symbol,order_id) WHERE l.order_id IS NULL').fetchone()[0] if {'execution_fills','execution_order_links'} <= names else None
        size = conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
        return {'backend':'postgresql','database_path':database_label(),'integrity':'connected',
                'file_bytes':size,'allocated_bytes':size,'reclaimable_bytes':None,'wal_bytes':None,
                'unlinked_fills':unlinked,'tables':rows,
                'size_note':'PostgreSQL 关系占用；SQLite 空闲页与 WAL 文件指标不适用。VACUUM 回收可复用空间，不保证文件缩小。'}


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
        cols = {r[1] for r in storage.table_columns(conn, table)}
        if CLEANABLE[table] not in cols:
            raise ValueError(f'{table} 不支持此版本清理')
        predicate = f'"{CLEANABLE[table]}" < %s'
        args = [cutoff.strftime('%Y-%m-%d %H:%M:%S')]
        if config_id:
            if 'config_id' not in cols:
                raise ValueError(f'{table} 是全局数据，不能按任务清理')
            predicate += ' AND config_id=%s'
            args.append(config_id)
        if table == 'short_memories':
            predicate += ' AND storage_id NOT IN (SELECT MAX(storage_id) FROM short_memories GROUP BY config_id)'
        if table == 'scheduler_runs':
            # Queue age is not permission to discard accepted work or its visible
            # run identity. The same predicate is rechecked during deletion.
            predicate += """ AND (status IS NULL OR status NOT IN ('QUEUED', 'RUNNING'))
                AND NOT EXISTS (SELECT 1 FROM agent_run_queue queued
                    WHERE queued.scheduler_run_id = scheduler_runs.id
                      AND queued.status IN ('QUEUED', 'CLAIMED', 'RUNNING'))"""
        digest.update(table.encode())
        count = 0
        with conn.cursor(name='cleanup_' + uuid.uuid4().hex) as cursor:
            cursor.execute(f'SELECT storage_id, xmin::text FROM "{table}" WHERE {predicate} ORDER BY storage_id', args)
            while batch := cursor.fetchmany(4096):
                for row in batch:
                    # UPDATE keeps storage_id but creates a new MVCC row version.
                    # Include that version without reading potentially large payloads.
                    digest.update(f'{row[0]}:{row[1]},'.encode())
                count += len(batch)
        selected[table] = {'count': count, 'predicate': predicate, 'args': args}
    return selected, digest.hexdigest()


def preview_cleanup(tables: list[str], before: str, config_id: str | None = None) -> dict:
    with database.get_db_conn() as conn:
        selected, token = _selection(conn, tables, before, config_id)
    return {'counts': {t: s['count'] for t, s in selected.items()}, 'preview_token': token,
            'notice': '将先备份再删除；历史分析和统计可能减少，成交账本与活跃保护保留。'}


def cleanup_targets() -> dict:
    """Include removed task IDs so legacy runtime data remains discoverable."""
    counts = {}
    with database.get_db_conn() as conn:
        names = storage.table_names(conn)
        active = {row[0] for row in conn.execute('SELECT config_id FROM agent_configs')} if 'agent_configs' in names else set()
        for table in CLEANABLE:
            if table not in names:
                continue
            columns = {row[1] for row in storage.table_columns(conn, table)}
            if 'config_id' not in columns:
                continue
            for task_id, count in conn.execute(f'SELECT config_id, COUNT(*) FROM "{table}" GROUP BY config_id'):
                if task_id:
                    counts[task_id] = counts.get(task_id, 0) + count
    return {'targets': [{'config_id': task_id, 'rows': count, 'orphaned': task_id not in active}
                        for task_id, count in sorted(counts.items(), key=lambda item: (item[0] in active, item[0]))]}


def _backup() -> str:
    from backend.storage.backup import dump_database
    directory = DATA_DIR / 'maintenance-backups'
    directory.mkdir(parents=True, exist_ok=True)
    name = f'{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.dump'
    dump_database(directory / name)
    return name


def clean_database(tables: list[str], before: str, preview_token: str, config_id: str | None = None) -> dict:
    with _maintenance_lock:
        preview = preview_cleanup(tables, before, config_id)
        if preview['preview_token'] != preview_token:
            raise ValueError('数据已变化，请重新预览')
        backup = _backup()
        with database.get_db_conn() as conn:
            storage.lock_scope(conn, 'database-maintenance')
            for table in sorted(set(tables)):
                if table not in CLEANABLE:
                    raise ValueError('Unsupported cleanup table')
                conn.execute(storage.sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(storage.sql.Identifier(table)))
            selected, token = _selection(conn, tables, before, config_id)
            if token != preview_token:
                raise ValueError('数据已变化，请重新预览')
            deleted = {t: conn.execute(f'DELETE FROM "{t}" WHERE {s["predicate"]}', s['args']).rowcount
                       for t, s in selected.items()}
            conn.execute('INSERT INTO database_maintenance_log(timestamp,payload) VALUES(%s,%s)',
                         (database._current_timestamp(), json.dumps({'deleted': deleted, 'backup': backup})))
            conn.commit()
        return {'deleted': deleted, 'backup': backup}


def compact_database() -> dict:
    with _maintenance_lock:
        with closing(storage.connect(autocommit=True)) as conn:
            before = conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
            conn.execute("SET statement_timeout = '90s'")
            conn.execute("SET lock_timeout = '2s'")
            conn.execute('VACUUM (ANALYZE)')
            after = conn.execute('SELECT pg_database_size(current_database())').fetchone()[0]
        return {'before_bytes':before,'after_bytes':after,'mode':'VACUUM ANALYZE',
                'notice':'空间可被 PostgreSQL 重用；不保证文件缩小，不执行 VACUUM FULL。'}


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
                            'completed': result.get('completed_count'), 'excluded': result.get('excluded_cycle_count'),
                            'scan_complete': result.get('scan_complete'), 'published': result.get('published'),
                            'reason_code': result.get('reason_code'),
                            'unmatched': result['unmatched_fill_count']})
        return {'backup': backup, 'results': results}
