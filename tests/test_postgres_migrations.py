"""Native PostgreSQL migration regressions using disposable test databases only.

Set TEST_POSTGRES_URL to a disposable PostgreSQL server with CREATEDB rights.
DATABASE_URL and project dotenv files are deliberately never consulted here.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import re
import shutil
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from backend.storage.migrate import MIGRATIONS_DIR, MIGRATION_LOCK, MigrationError, check_schema, migrate


def test_startup_rejects_a_different_business_schema_before_version_check():
    class OtherSchema:
        def execute(self, statement):
            assert statement == 'SELECT current_schema()'
            return self

        def fetchone(self):
            return ('other',)

    with pytest.raises(MigrationError, match='must be public'):
        check_schema(OtherSchema())


@pytest.fixture
def blank_database():
    dsn = os.getenv('TEST_POSTGRES_URL')
    if not dsn:
        pytest.skip('Set explicit TEST_POSTGRES_URL to a disposable PostgreSQL server')
    # Create and remember the exact database; never drop the supplied database.
    name = 'crypto_migration_test_' + uuid.uuid4().hex
    settings = conninfo_to_dict(dsn)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    test_dsn = make_conninfo(**{**settings, 'dbname': name})
    try:
        yield test_dsn
    finally:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def ready_database(blank_database):
    migrate(dsn=blank_database)
    return blank_database


def test_empty_database_covers_complete_baseline_and_repeat_is_noop(blank_database):
    first = migrate(dsn=blank_database)
    assert first['status'] == 'complete'
    assert len(first['applied']) >= 2
    with psycopg.connect(blank_database) as conn:
        check_schema(conn)
        expected = set()
        for file in MIGRATIONS_DIR.glob('*.sql'):
            expected.update(re.findall(r'^CREATE TABLE (\w+)\s*\(', file.read_text(), flags=re.M))
        actual = {row[0] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert expected <= actual
        assert len(expected) >= 110
        conn.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES('preserve','{\"text\":\"中文\"}','now')")
        conn.commit()
    assert migrate(dsn=blank_database)['applied'] == []
    with psycopg.connect(blank_database) as conn:
        assert conn.execute("SELECT value_json FROM app_settings WHERE key='preserve'").fetchone()[0] == '{"text":"中文"}'
        assert conn.execute("SELECT status FROM schema_migration_stages WHERE stage='checkpoints'").fetchone()[0] == 'complete'


def test_migration_uses_public_even_if_connection_default_is_another_schema(blank_database):
    with psycopg.connect(blank_database) as conn:
        conn.execute('CREATE SCHEMA misdirected')
    other_dsn = make_conninfo(blank_database, options='-c search_path=misdirected')
    assert migrate(dsn=other_dsn)['status'] == 'complete'
    with psycopg.connect(blank_database) as conn:
        check_schema(conn)
        assert conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname='misdirected'").fetchone()[0] == 0
    with psycopg.connect(other_dsn) as conn:
        with pytest.raises(MigrationError, match='must be public'):
            check_schema(conn)


def test_types_and_generated_identity_are_explicit(ready_database):
    with psycopg.connect(ready_database) as conn:
        payload = '{"value":"中文\\u0000","float":0.10000000000000002}'
        stamp = 2**42
        identity = conn.execute('''INSERT INTO execution_fills(account_scope,symbol,trade_id,timestamp_ms,payload)
            VALUES('scope','BTC','trade',%s,%s) RETURNING storage_id''', (stamp, payload)).fetchone()[0]
        assert isinstance(identity, int) and identity > 0
        assert conn.execute('SELECT timestamp_ms,payload FROM execution_fills WHERE storage_id=%s', (identity,)).fetchone() == (stamp, payload)
        explicit = conn.execute("INSERT INTO orders(order_id,entry_price) VALUES('o',%s) RETURNING id,storage_id", (0.10000000000000002,)).fetchone()
        assert explicit[0] == explicit[1]
        assert conn.execute('SELECT entry_price FROM orders WHERE id=%s', (explicit[0],)).fetchone()[0] == 0.10000000000000002
        assert conn.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='checkpoints' AND table_name='checkpoint_blobs' AND column_name='blob'").fetchone()[0] == 'bytea'
        assert conn.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='execution_fills' AND column_name='timestamp_ms'").fetchone()[0] == 'bigint'
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute("INSERT INTO execution_fills(account_scope,symbol,trade_id,timestamp_ms,payload) VALUES('scope','BTC','trade',1,'{}')")


def test_failed_sql_rolls_back_only_its_version_and_can_retry(blank_database, tmp_path):
    for path in MIGRATIONS_DIR.glob('*.sql'):
        shutil.copyfile(path, tmp_path / path.name)
    number = len(list(tmp_path.glob('*.sql'))) + 1
    failing = tmp_path / f'{number:04d}_failure_probe.sql'
    failing.write_text('CREATE TABLE migration_probe(id BIGINT); SELECT 1/0;', encoding='utf-8')
    with pytest.raises(MigrationError, match='failure_probe'):
        migrate(dsn=blank_database, directory=tmp_path)
    with psycopg.connect(blank_database) as conn:
        assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == number - 1
        assert conn.execute("SELECT to_regclass('public.migration_probe')").fetchone()[0] is None
        with pytest.raises(MigrationError):
            check_schema(conn, directory=tmp_path)
    failing.write_text('CREATE TABLE migration_probe(id BIGINT);', encoding='utf-8')
    assert migrate(dsn=blank_database, directory=tmp_path)['applied'] == [failing.name]


def test_applied_checksum_change_is_rejected(ready_database, tmp_path):
    for path in MIGRATIONS_DIR.glob('*.sql'):
        shutil.copyfile(path, tmp_path / path.name)
    first = sorted(tmp_path.glob('*.sql'))[0]
    first.write_text(first.read_text(encoding='utf-8') + '\n-- forbidden edit\n', encoding='utf-8')
    with pytest.raises(MigrationError, match='checksum mismatch'):
        migrate(dsn=ready_database, directory=tmp_path)


def test_windows_line_endings_do_not_change_schema_identity(ready_database, tmp_path):
    for path in MIGRATIONS_DIR.glob('*.sql'):
        content = path.read_text(encoding='utf-8').replace('\n', '\r\n')
        (tmp_path / path.name).write_bytes(content.encode('utf-8'))
    assert migrate(dsn=ready_database, directory=tmp_path)['applied'] == []


def test_checkpoint_failure_is_recorded_and_retry_finishes(blank_database):
    def fail(_):
        raise RuntimeError('sensitive test failure must not enter deployment output')

    with pytest.raises(MigrationError, match='stage checkpoints') as error:
        migrate(dsn=blank_database, checkpoint_setup=fail)
    assert 'sensitive' not in str(error.value)
    with psycopg.connect(blank_database) as conn:
        assert conn.execute("SELECT status FROM schema_migration_stages WHERE stage='checkpoints'").fetchone()[0] == 'failed'
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] >= 2
        with pytest.raises(MigrationError, match='Checkpoint schema'):
            check_schema(conn)
    assert migrate(dsn=blank_database)['status'] == 'complete'


def test_two_migrators_serialize_and_validate_same_versions(blank_database):
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: migrate(dsn=blank_database), range(2)))
    assert all(result['status'] == 'complete' for result in results)
    applied = [name for result in results for name in result['applied']]
    assert len(applied) == len(set(applied))
    assert len(applied) == len(list(MIGRATIONS_DIR.glob('*.sql')))


def test_migration_lock_wait_is_bounded(blank_database):
    with psycopg.connect(blank_database, autocommit=True) as owner:
        owner.execute('SELECT pg_advisory_lock(%s)', (MIGRATION_LOCK,))
        with pytest.raises(MigrationError, match='migration lock'):
            migrate(dsn=blank_database, lock_timeout=0.05)
        owner.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK,))


def test_protection_projection_is_transactional_and_invalid_stays_unknown(ready_database):
    with psycopg.connect(ready_database) as conn:
        key = conn.execute('''INSERT INTO real_protection_plans(config_id,symbol,side,payload)
            VALUES('c','BTC','LONG','{"state":"LIVE","updated_at":42.25}') RETURNING storage_id''').fetchone()[0]
        assert conn.execute('SELECT state,updated_at FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone() == ('LIVE', 42.25)
        conn.commit()
        conn.execute("UPDATE real_protection_plans SET payload='invalid JSON' WHERE storage_id=%s", (key,))
        assert conn.execute('SELECT state FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone()[0] == 'UNKNOWN'
        conn.rollback()
        assert conn.execute('SELECT state FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone()[0] == 'LIVE'
        conn.execute("SELECT set_config('crypto.max_record_bytes','8',true)")
        conn.execute("UPDATE real_protection_plans SET payload='{\"state\":\"DONE\"}' WHERE storage_id=%s", (key,))
        assert conn.execute('SELECT state FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone()[0] == 'DONE'
        conn.execute('DELETE FROM real_protection_plans WHERE storage_id=%s', (key,))
        assert conn.execute('SELECT 1 FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone() is None


def test_unprojectable_json_does_not_reject_or_rewrite_raw_text(ready_database):
    payload = r'{"state":"LIVE","account_scope":"a","symbol":"BTC","memo":"\u0000"}'
    with psycopg.connect(ready_database) as conn:
        key = conn.execute('''INSERT INTO real_protection_plans(config_id,symbol,side,payload)
            VALUES('c','BTC','LONG',%s) RETURNING storage_id''', (payload,)).fetchone()[0]
        assert conn.execute('SELECT payload FROM real_protection_plans WHERE storage_id=%s', (key,)).fetchone()[0] == payload
        assert conn.execute('SELECT state FROM protection_plan_activity WHERE plan_rowid=%s', (key,)).fetchone()[0] == 'UNKNOWN'
        event = conn.execute('''INSERT INTO real_protection_events(timestamp,config_id,symbol,payload)
            VALUES('now','c','BTC',%s) RETURNING id''', (payload,)).fetchone()[0]
        assert conn.execute('SELECT payload FROM real_protection_events WHERE id=%s', (event,)).fetchone()[0] == payload


def test_source_revisions_cover_late_identity_update_delete_and_scope(ready_database):
    with psycopg.connect(ready_database) as late, psycopg.connect(ready_database) as first:
        lower = late.execute("SELECT nextval(pg_get_serial_sequence('execution_fills','storage_id'))").fetchone()[0]
        higher = first.execute("INSERT INTO execution_fills(account_scope,symbol,trade_id,timestamp_ms,payload) VALUES('a','BTC','high',1,'{}') RETURNING storage_id").fetchone()[0]
        first.commit()
        assert lower < higher
        before = first.execute("SELECT revision FROM execution_source_revisions WHERE account_scope='a' AND symbol='BTC'").fetchone()[0]
        first.commit()
        late.execute("INSERT INTO execution_fills(storage_id,account_scope,symbol,trade_id,timestamp_ms,payload) VALUES(%s,'a','BTC','low',1,'{}')", (lower,))
        late.commit()
        assert first.execute("SELECT revision FROM execution_source_revisions WHERE account_scope='a' AND symbol='BTC'").fetchone()[0] > before
        first.execute("INSERT INTO execution_fills(account_scope,symbol,trade_id,timestamp_ms,payload) VALUES('unrelated','ETH','x',1,'{}')")
        first.commit()
        assert first.execute("SELECT revision FROM execution_source_revisions WHERE account_scope='a' AND symbol='BTC'").fetchone()[0] == before + 1
        first.execute("UPDATE execution_fills SET payload='updated' WHERE storage_id=%s", (lower,))
        first.execute('DELETE FROM execution_fills WHERE storage_id=%s', (lower,))
        assert first.execute("SELECT revision FROM execution_source_revisions WHERE account_scope='a' AND symbol='BTC'").fetchone()[0] == before + 3


def test_paper_raw_and_scan_versions_share_source_transaction(ready_database):
    with psycopg.connect(ready_database) as conn:
        conn.execute("INSERT INTO paper_accounts(namespace,user_id,payload) VALUES('n','u','{}')")
        assert conn.execute("SELECT revision FROM paper_raw_versions WHERE namespace='n' AND user_id='u' AND kind='account'").fetchone()[0] == 1
        conn.execute("UPDATE paper_accounts SET payload='[]' WHERE namespace='n' AND user_id='u'")
        assert conn.execute("SELECT revision FROM paper_raw_versions WHERE namespace='n' AND user_id='u' AND kind='account'").fetchone()[0] == 2
        conn.execute("INSERT INTO paper_operations(namespace,user_id,operation_id,fingerprint,principal,result) VALUES('n','u','op','fp','user','{}')")
        assert conn.execute("SELECT revision FROM paper_source_revisions WHERE namespace='n' AND user_id='u'").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM paper_raw_versions WHERE namespace='n'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM paper_source_revisions WHERE namespace='n'").fetchone()[0] == 0
