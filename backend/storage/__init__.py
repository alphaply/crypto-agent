"""Native PostgreSQL connections; no SQLite fallback or SQL rewriting.

Borrowed connections require an explicit commit. Uncommitted work is rolled
back before returning to the pool, including on normal context exit.
"""
from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg
from psycopg import Connection, Cursor, DatabaseError, Error, IntegrityError, OperationalError, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool


class StorageConfigurationError(ValueError):
    """A database configuration is missing or invalid (never includes the DSN)."""


class Row(tuple):
    """A result with positional iteration and explicit column-name lookup."""

    def __new__(cls, names, values):
        result = super().__new__(cls, values)
        result._names = tuple(names)
        result._positions = {name: i for i, name in reversed(list(enumerate(names)))}
        return result

    def __getitem__(self, key):
        return super().__getitem__(self._positions[key] if isinstance(key, str) else key)

    def keys(self):
        return self._names

    def get(self, key, default=None):
        return self[key] if key in self._positions else default


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def record(row, **extra) -> dict:
    """Project business columns without exposing the internal scan identity."""
    result = dict(row)
    result.pop('storage_id', None)
    result.update(extra)
    return result


def database_url() -> str:
    value = os.environ.get('DATABASE_URL', '').strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'postgresql', 'postgres'} or not parsed.hostname or not parsed.path.strip('/'):
            raise ValueError
        conninfo_to_dict(value)
    except Exception:
        raise StorageConfigurationError('DATABASE_URL must be an explicit PostgreSQL URL with a host and database') from None
    return value


def database_label() -> str:
    """A non-secret label for diagnostics (password/query/user never included)."""
    parsed = urlsplit(database_url())
    return f'postgresql://{parsed.hostname}:{parsed.port or 5432}{parsed.path}'


def _positive(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise StorageConfigurationError(f'{name} must be a finite positive number') from None


_pool: ConnectionPool | None = None
_pool_pid: int | None = None
_pool_guard = threading.Lock()


def open_pool() -> ConnectionPool:
    global _pool, _pool_pid
    with _pool_guard:
        if _pool is not None and _pool_pid == os.getpid():
            return _pool
        size = _positive('DB_POOL_MAX_SIZE', 8)
        if not size.is_integer():
            raise StorageConfigurationError('DB_POOL_MAX_SIZE must be an integer')
        _pool = ConnectionPool(
            conninfo=database_url(), min_size=0, max_size=int(size), open=False,
            timeout=_positive('DB_POOL_TIMEOUT_SECONDS', 10),
            max_waiting=int(size), reconnect_timeout=5,
            kwargs={'row_factory': row_factory, 'connect_timeout': 5,
                    'application_name': 'crypto-agent'},
        )
        _pool.open()
        _pool_pid = os.getpid()
        return _pool


def close_pool() -> None:
    global _pool, _pool_pid
    with _pool_guard:
        pool, _pool, _pool_pid = _pool, None, None
    if pool:
        pool.close()


def connect(*, timeout: float = 30, autocommit: bool = False) -> Connection:
    """Standalone connection; caller owns commit/rollback and close."""
    connection = psycopg.connect(database_url(), connect_timeout=max(1, math.ceil(timeout)),
                           autocommit=autocommit, row_factory=row_factory,
                           application_name='crypto-agent')
    try:
        _capacity_settings(connection)
        if not autocommit:
            connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def _capacity_settings(conn):
    # Compatibility with pre-0006 triggers during an explicit schema upgrade.
    conn.execute("SELECT set_config('crypto.capacity_enabled', 'false', false)")


@contextmanager
def get_connection(*, timeout: float = 30):
    if not math.isfinite(timeout) or timeout <= 0:
        raise OperationalError('Database request budget exhausted')
    started = time.monotonic()
    wait = min(timeout, _positive('DB_POOL_TIMEOUT_SECONDS', 10))
    if wait <= 0:
        raise OperationalError('Database request budget exhausted')
    pool = open_pool()
    conn = pool.getconn(timeout=wait)
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise OperationalError('Database request budget exhausted')
        # Session settings survive explicit commits; reset them before reuse.
        conn.autocommit = True
        milliseconds = str(max(1, int(remaining * 1000)))
        conn.execute("SELECT set_config('lock_timeout', %s, false), "
                     "set_config('statement_timeout', %s, false), "
                     "set_config('crypto.capacity_enabled', 'false', false)",
                     (milliseconds, milliseconds))
        conn.autocommit = False
        yield conn
    finally:
        if not conn.closed:
            try:
                conn.rollback()
                conn.autocommit = True
                # NULL has RESET semantics, including role/database defaults.
                conn.execute("SELECT set_config('lock_timeout', NULL, false), "
                             "set_config('statement_timeout', NULL, false)")
                conn.autocommit = False
            except psycopg.Error:
                conn.close()
        pool.putconn(conn)


def lock_scope(conn: Connection, *parts: object) -> None:
    """Transaction-scoped coordination for an explicitly selected business scope."""
    identity = '\0'.join(str(part) for part in parts).encode('utf-8')
    key = int.from_bytes(hashlib.sha256(identity).digest()[:8], 'big', signed=True)
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (key,))


def try_lock_scope(conn: Connection, *parts: object) -> bool:
    """Take the same scope without waiting, for opportunistic account repair."""
    identity = '\0'.join(str(part) for part in parts).encode('utf-8')
    key = int.from_bytes(hashlib.sha256(identity).digest()[:8], 'big', signed=True)
    return conn.execute('SELECT pg_try_advisory_xact_lock(%s)', (key,)).fetchone()[0]


def table_names(conn: Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=current_schema()")}


def table_columns(conn: Connection, table: str) -> list[Row]:
    return conn.execute('''SELECT a.attnum - 1 AS cid, a.attname AS name,
        pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
        a.attnotnull::int AS notnull, pg_get_expr(d.adbin,d.adrelid) AS dflt_value,
        COALESCE(array_position(i.indkey::smallint[], a.attnum) + 1, 0) AS pk
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisprimary
        WHERE n.nspname=current_schema() AND c.relname=%s
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum''', (table,)).fetchall()


def table_indexes(conn: Connection, table: str) -> list[Row]:
    return conn.execute('''SELECT row_number() OVER (ORDER BY ci.relname)-1 AS seq,
        ci.relname AS name, i.indisunique::int AS "unique", i.indisprimary AS "primary",
        i.indpred IS NOT NULL AS partial
        FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
        JOIN pg_class ci ON ci.oid=i.indexrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=current_schema() AND t.relname=%s''', (table,)).fetchall()


def index_columns(conn: Connection, name: str) -> list[Row]:
    return conn.execute('''SELECT k.ordinality-1 AS seqno,a.attnum AS cid,a.attname AS name
        FROM pg_index i JOIN pg_class ix ON ix.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=ix.relnamespace
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ordinality)
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
        WHERE n.nspname=current_schema() AND ix.relname=%s ORDER BY k.ordinality''', (name,)).fetchall()
