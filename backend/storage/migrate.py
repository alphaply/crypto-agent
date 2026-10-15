"""Explicit PostgreSQL schema upgrades, independent of application imports.

Run ``python -m backend.storage.migrate`` before starting either application
process. Each SQL file and its version record commit together. A session lock
also covers the separately committed LangGraph migrations.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable

import psycopg
from psycopg.rows import dict_row

from backend.storage import database_url


MIGRATIONS_DIR = Path(__file__).with_name('migrations')
MIGRATION_LOCK = 0x43525950544F5047
CHECKPOINT_SCHEMA = 'checkpoints'


class MigrationError(RuntimeError):
    """A safe deployment error that never embeds a connection string."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    result = []
    for path in sorted(directory.glob('*.sql')):
        match = re.fullmatch(r'(\d{4})_([a-z0-9_]+)\.sql', path.name)
        if not match:
            raise MigrationError('Invalid migration filename: ' + path.name)
        # Git can check the same source out as CRLF on Windows and LF in Linux
        # image builds. Normalize only line endings; all SQL content is hashed.
        statement = path.read_text(encoding='utf-8')
        result.append(Migration(int(match[1]), path.name, hashlib.sha256(statement.encode('utf-8')).hexdigest(), statement))
    versions = [item.version for item in result]
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise MigrationError('Application migrations must be numbered consecutively from 0001')
    return result


def checkpoint_signature() -> tuple[str, str, int]:
    from langgraph.checkpoint.postgres import PostgresSaver

    version = importlib.metadata.version('langgraph-checkpoint-postgres')
    checksum = hashlib.sha256('\n'.join(PostgresSaver.MIGRATIONS).encode('utf-8')).hexdigest()
    return version, checksum, len(PostgresSaver.MIGRATIONS) - 1


def _metadata(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS public.schema_migrations (
        version BIGINT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS public.schema_migration_stages (
        stage TEXT PRIMARY KEY, version TEXT NOT NULL, checksum TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def _verify_application(conn, migrations: list[Migration], *, complete: bool) -> set[int]:
    rows = conn.execute('SELECT version,name,checksum FROM public.schema_migrations ORDER BY version').fetchall()
    expected = {item.version: item for item in migrations}
    applied = set()
    for version, name, checksum in rows:
        migration = expected.get(version)
        if migration is None:
            raise MigrationError('Database schema is newer than this application; use a compatible image')
        if (name, checksum) != (migration.name, migration.checksum):
            raise MigrationError(f'Migration checksum mismatch at version {version}; do not edit applied files')
        applied.add(version)
    if applied and applied != set(range(1, max(applied) + 1)):
        raise MigrationError('Application schema migration history has a gap')
    if complete and applied != set(expected):
        raise MigrationError('Application schema is not ready; run python -m backend.storage.migrate')
    return applied


def _check_checkpoints(conn) -> None:
    version, checksum, latest = checkpoint_signature()
    stage = conn.execute("SELECT version,checksum,status FROM public.schema_migration_stages WHERE stage='checkpoints'").fetchone()
    if stage is None or tuple(stage) != (version, checksum, 'complete'):
        raise MigrationError('Checkpoint schema is not ready; run python -m backend.storage.migrate')
    applied = conn.execute('SELECT max(v) FROM checkpoints.checkpoint_migrations').fetchone()[0]
    if applied != latest:
        raise MigrationError('Checkpoint migration history does not match the installed dependency')


def check_schema(conn, *, directory: Path = MIGRATIONS_DIR) -> None:
    """Read-only readiness check; callers retain transaction ownership."""
    if conn.execute('SELECT current_schema()').fetchone()[0] != 'public':
        raise MigrationError('Application business schema must be public; correct the database role or connection search_path')
    try:
        _verify_application(conn, load_migrations(directory), complete=True)
        _check_checkpoints(conn)
    except psycopg.errors.UndefinedTable:
        raise MigrationError('Database schema is missing; run python -m backend.storage.migrate') from None


def _setup_checkpoints(dsn: str) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    # LangGraph creates indexes CONCURRENTLY, requiring autocommit. Its setup
    # commits progress per version and is safe to resume after a partial error.
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                         options='-c search_path=checkpoints', connect_timeout=15) as conn:
        PostgresSaver(conn).setup()


def migrate(*, dsn: str | None = None, directory: Path = MIGRATIONS_DIR,
            checkpoint_setup: Callable[[str], None] | None = None,
            lock_timeout: float = 60) -> dict:
    """Apply all pending versions. The optional hooks support isolated tests."""
    dsn = dsn or database_url()
    migrations = load_migrations(directory)
    completed = []
    stage = 'connection'
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
            conn.execute('SET search_path TO public')
            conn.execute("SELECT set_config('lock_timeout', %s, false)", (f'{max(1, int(lock_timeout * 1000))}ms',))
            stage = 'migration lock'
            # A blocking advisory SELECT retains a transaction snapshot. It
            # would prevent the lock owner's checkpoint CREATE INDEX
            # CONCURRENTLY from completing, so wait between autocommit queries.
            deadline = time.monotonic() + max(0, lock_timeout)
            while not conn.execute('SELECT pg_try_advisory_lock(%s)', (MIGRATION_LOCK,)).fetchone()[0]:
                if time.monotonic() >= deadline:
                    raise MigrationError('Migration stage migration lock timed out; another deployment is running')
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            try:
                with conn.transaction():
                    _metadata(conn)
                applied = _verify_application(conn, migrations, complete=False)
                for migration in migrations:
                    if migration.version in applied:
                        continue
                    stage = migration.name
                    with conn.transaction():
                        conn.execute(migration.sql, prepare=False)
                        conn.execute('INSERT INTO public.schema_migrations(version,name,checksum) VALUES(%s,%s,%s)',
                                     (migration.version, migration.name, migration.checksum))
                    completed.append(migration.name)
                stage = 'checkpoints'
                version, checksum, _ = checkpoint_signature()
                record = conn.execute("SELECT version,checksum,status FROM public.schema_migration_stages WHERE stage='checkpoints'").fetchone()
                if record != (version, checksum, 'complete'):
                    conn.execute("""INSERT INTO public.schema_migration_stages(stage,version,checksum,status)
                        VALUES('checkpoints',%s,%s,'running') ON CONFLICT(stage) DO UPDATE SET
                        version=EXCLUDED.version,checksum=EXCLUDED.checksum,status='running',updated_at=CURRENT_TIMESTAMP""",
                                 (version, checksum))
                    conn.execute('CREATE SCHEMA IF NOT EXISTS checkpoints')
                    try:
                        (checkpoint_setup or _setup_checkpoints)(dsn)
                        from langgraph.checkpoint.postgres import PostgresSaver
                        highest = conn.execute('SELECT max(v) FROM checkpoints.checkpoint_migrations').fetchone()[0]
                        if highest != len(PostgresSaver.MIGRATIONS) - 1:
                            raise MigrationError('Checkpoint setup did not finish every dependency migration')
                    except Exception:
                        conn.execute("UPDATE public.schema_migration_stages SET status='failed',updated_at=CURRENT_TIMESTAMP WHERE stage='checkpoints'")
                        raise
                    conn.execute("UPDATE public.schema_migration_stages SET status='complete',updated_at=CURRENT_TIMESTAMP WHERE stage='checkpoints'")
                check_schema(conn, directory=directory)
                return {'status': 'complete', 'applied': completed, 'schema_version': migrations[-1].version,
                        'checkpoints': version}
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK,))
    except MigrationError:
        raise
    except Exception as exc:
        # Diagnostics can contain failed statements and values. Only bounded,
        # non-sensitive metadata enters the deployment output.
        code = getattr(exc, 'sqlstate', None)
        reason = f'{type(exc).__name__}' + (f' SQLSTATE {code}' if code else '')
        raise MigrationError(f'Migration stage {stage} failed ({reason}); completed application versions: {completed}') from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='check readiness without changing any schema')
    args = parser.parse_args(argv)
    # CLI convenience only: importing this module remains free of dotenv and
    # business side effects. Explicit process/Compose values take precedence.
    from dotenv import load_dotenv
    load_dotenv(Path.cwd() / '.env', override=False)
    try:
        if args.check:
            with psycopg.connect(database_url(), connect_timeout=15) as conn:
                check_schema(conn)
            result = {'status': 'ready'}
        else:
            result = migrate()
        print(json.dumps(result))
        return 0
    except (MigrationError, psycopg.Error, ValueError) as exc:
        message = str(exc) if isinstance(exc, (MigrationError, ValueError)) else type(exc).__name__
        print('PostgreSQL migration failed: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
