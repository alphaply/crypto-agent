"""Batched connection settings preserve real PostgreSQL session boundaries."""
import os
import time
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from backend import storage


@pytest.fixture
def configured_pg(monkeypatch):
    target = os.getenv('TEST_POSTGRES_URL')
    if not target:
        pytest.skip('Set TEST_POSTGRES_URL to an isolated PostgreSQL database')
    parsed = urlsplit(target)
    if 'test' not in parsed.path.lower():
        pytest.fail('Connection settings tests require a disposable test database')
    identity = uuid4().hex
    role = 'connection_role_test_' + identity
    database = 'connection_defaults_test_' + identity
    password = 'temporary-connection-test-only'
    storage.close_pool()
    with psycopg.connect(target, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        try:
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(database), sql.Identifier(role)))
            # The role override and the database-only value must both survive
            # RESET; hard-coding either timeout to zero would fail these tests.
            admin.execute(sql.SQL("ALTER DATABASE {} SET lock_timeout = '6100ms'").format(sql.Identifier(database)))
            admin.execute(sql.SQL("ALTER DATABASE {} SET statement_timeout = '6200ms'").format(sql.Identifier(database)))
            admin.execute(sql.SQL("ALTER ROLE {} SET lock_timeout = '4300ms'").format(sql.Identifier(role)))
            netloc = quote(role) + ':' + quote(password) + '@' + parsed.netloc.rsplit('@', 1)[-1]
            address = urlunsplit(parsed._replace(netloc=netloc, path='/' + database))
            monkeypatch.setenv('DATABASE_URL', address)
            monkeypatch.setenv('DB_POOL_MAX_SIZE', '1')
            yield address
        finally:
            storage.close_pool()
            admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
            admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def settings(conn):
    return conn.execute(
        "SELECT current_setting('crypto.capacity_enabled') AS capacity_enabled, "
        "(SELECT setting::int FROM pg_settings WHERE name='lock_timeout') AS lock_ms, "
        "(SELECT setting::int FROM pg_settings WHERE name='statement_timeout') AS statement_ms, "
        "pg_backend_pid() AS pid"
    ).fetchone()


def assert_returned_defaults(pid):
    # Inspect the already-pooled session before get_connection reapplies its
    # next request budget, so a leaked setting cannot be hidden by checkout.
    with storage.open_pool().connection() as conn:
        current = settings(conn)
        assert current['pid'] == pid
        assert current['lock_ms'] == 4300
        assert current['statement_ms'] == 6200


def test_batched_settings_survive_commits_and_reborrowing_clears_changed_capacity_flag(configured_pg):
    with storage.get_connection(timeout=.8) as conn:
        initial = settings(conn)
        assert 1 <= initial['lock_ms'] == initial['statement_ms'] <= 800
        assert initial['capacity_enabled'] == 'false'
        conn.execute('CREATE TEMP TABLE connection_settings_commit (id integer)')
        conn.commit()
        assert settings(conn) == initial
        conn.execute('INSERT INTO connection_settings_commit VALUES (1)')
        conn.commit()
        conn.execute('INSERT INTO connection_settings_commit VALUES (2)')
        conn.rollback()
        assert settings(conn) == initial
        assert conn.execute('SELECT id FROM connection_settings_commit').fetchall() == [(1,)]
        conn.execute("SELECT set_config('crypto.capacity_enabled', 'true', false)")
        conn.commit()
    assert_returned_defaults(initial['pid'])
    with storage.get_connection(timeout=1.2) as conn:
        current = settings(conn)
        assert current['pid'] == initial['pid']
        assert current['capacity_enabled'] == 'false'
        assert 1 <= current['lock_ms'] == current['statement_ms'] <= 1200
    assert_returned_defaults(initial['pid'])


def test_exception_rolls_back_writes_and_restores_nonzero_defaults(configured_pg):
    with storage.get_connection() as conn:
        pid = settings(conn)['pid']
        conn.execute('CREATE TEMP TABLE connection_settings_rollback (id integer)')
        conn.commit()
    with pytest.raises(RuntimeError, match='abort the transaction'):
        with storage.get_connection(timeout=.8) as conn:
            conn.execute('INSERT INTO connection_settings_rollback VALUES (1)')
            conn.execute("SELECT set_config('crypto.capacity_enabled', 'true', false)")
            raise RuntimeError('abort the transaction')
    assert_returned_defaults(pid)
    with storage.get_connection() as conn:
        assert settings(conn)['capacity_enabled'] == 'false'
        assert conn.execute('SELECT count(*) FROM connection_settings_rollback').fetchone()[0] == 0


def test_statement_timeout_still_cancels_rolls_back_and_returns_reusable_session(configured_pg):
    with storage.get_connection() as conn:
        pid = settings(conn)['pid']
        conn.execute('CREATE TEMP TABLE connection_settings_timeout (id integer)')
        conn.commit()
    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        with storage.get_connection(timeout=.1) as conn:
            conn.execute('INSERT INTO connection_settings_timeout VALUES (1)')
            conn.execute('SELECT pg_sleep(1)')
    assert time.monotonic() - started < 2
    assert_returned_defaults(pid)
    with storage.get_connection() as conn:
        assert settings(conn)['capacity_enabled'] == 'false'
        assert conn.execute('SELECT count(*) FROM connection_settings_timeout').fetchone()[0] == 0


def test_lock_timeout_survives_commit_and_restores_both_role_database_defaults(configured_pg):
    with psycopg.connect(configured_pg) as blocker:
        storage.lock_scope(blocker, 'connection-settings-lock')
        with storage.get_connection() as conn:
            pid = settings(conn)['pid']
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with storage.get_connection(timeout=.1) as conn:
                # Isolate lock_timeout from the equally short statement limit.
                conn.execute("SELECT set_config('statement_timeout', '5s', false)")
                conn.commit()
                storage.lock_scope(conn, 'connection-settings-lock')
        assert time.monotonic() - started < 2
    assert_returned_defaults(pid)
    with storage.get_connection() as conn:
        assert settings(conn)['capacity_enabled'] == 'false'
        storage.lock_scope(conn, 'connection-settings-lock')
