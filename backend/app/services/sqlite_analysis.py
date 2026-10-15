"""Read-only analysis of an explicitly uploaded SQLite copy."""
import sqlite3
from contextlib import closing
from pathlib import Path

def inspect_sqlite_copy(path: str | Path) -> dict:
    target = Path(path).resolve()
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
            time_col = next((c for c in [{}.get(name), 'timestamp_ms', 'timestamp', 'closed_at_ms', 'created_at'] if c in columns), None)
            span = conn.execute(f'SELECT MIN("{time_col}"),MAX("{time_col}") FROM {quoted}').fetchone() if time_col else (None, None)
            rows.append({'table': name, 'rows': conn.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0],
                         'bytes': sizes.get(name), 'oldest': span[0], 'newest': span[1],
                         'task_scoped': 'config_id' in columns, 'cleanable': name in {} and {}[name] in columns})
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
