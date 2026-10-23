import threading
import time

import pytest

from backend import storage


@pytest.fixture
def pg(monkeypatch):
    import os
    url = os.getenv('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set TEST_POSTGRES_URL to an isolated PostgreSQL database')
    storage.close_pool()
    monkeypatch.setenv('DATABASE_URL', url)
    yield
    storage.close_pool()


@pytest.mark.parametrize('url', ['', 'sqlite:///x.db', 'postgresql://host', 'mysql://user:secret@host/db'])
def test_invalid_url_never_contains_secret(monkeypatch, url):
    monkeypatch.setenv('DATABASE_URL', url)
    with pytest.raises(storage.StorageConfigurationError) as error:
        storage.database_url()
    assert 'secret' not in str(error.value)


def test_url_password_characters_and_safe_label(monkeypatch):
    value = 'postgresql://name:p%40ss%3A%2F%25@localhost:5432/example?sslmode=require'
    monkeypatch.setenv('DATABASE_URL', value)
    assert storage.database_url() == value
    assert storage.database_label() == 'postgresql://localhost:5432/example'


def test_explicit_commit_and_rollback(pg):
    with storage.get_connection() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS storage_connection_test (id bigint PRIMARY KEY)')
        conn.execute('DELETE FROM storage_connection_test')
        conn.commit()
    with storage.get_connection() as conn:
        conn.execute('INSERT INTO storage_connection_test VALUES (1)')
    with storage.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM storage_connection_test').fetchone()[0] == 0
        conn.execute('INSERT INTO storage_connection_test VALUES (2)')
        conn.commit()
    with pytest.raises(RuntimeError):
        with storage.get_connection() as conn:
            conn.execute('INSERT INTO storage_connection_test VALUES (3)')
            raise RuntimeError('rollback')
    with storage.get_connection() as conn:
        row = conn.execute('SELECT id FROM storage_connection_test').fetchone()
        assert row[0] == row['id'] == 2
        assert dict(row) == {'id': 2}
        conn.execute('DROP TABLE storage_connection_test')
        conn.commit()


def test_lock_timeout_releases_connection(pg):
    with storage.get_connection() as first:
        storage.lock_scope(first, 'connection-test')
        started = time.monotonic()
        with pytest.raises(storage.OperationalError):
            with storage.get_connection(timeout=0.1) as second:
                storage.lock_scope(second, 'connection-test')
        assert time.monotonic() - started < 3
    with storage.get_connection() as conn:
        assert conn.execute('SELECT 42').fetchone()[0] == 42


def test_pool_is_bounded(pg, monkeypatch):
    storage.close_pool()
    monkeypatch.setenv('DB_POOL_MAX_SIZE', '1')
    with storage.get_connection() as conn:
        from psycopg_pool import PoolTimeout
        with pytest.raises(PoolTimeout):
            with storage.get_connection(timeout=0.05):
                pass
    assert storage.open_pool().get_stats()['pool_size'] == 1


def test_disconnect_fails_original_transaction_and_next_request_recovers(pg):
    import os
    import psycopg
    with pytest.raises(storage.OperationalError):
        with storage.get_connection() as conn:
            pid = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
            with psycopg.connect(os.environ['TEST_POSTGRES_URL'], autocommit=True) as admin:
                admin.execute('SELECT pg_terminate_backend(%s)', (pid,))
            conn.execute('SELECT 1')
    with storage.get_connection() as conn:
        assert conn.execute('SELECT 2').fetchone()[0] == 2


def test_standalone_configuration_failure_closes_connection(monkeypatch):
    from unittest.mock import Mock
    connection = Mock()
    monkeypatch.setattr(storage.psycopg, 'connect', lambda *args, **kwargs: connection)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://localhost/test')
    def invalid_settings(_connection):
        raise storage.StorageConfigurationError('Invalid connection settings')
    monkeypatch.setattr(storage, '_capacity_settings', invalid_settings)
    with pytest.raises(storage.StorageConfigurationError):
        storage.connect()
    connection.close.assert_called_once()
