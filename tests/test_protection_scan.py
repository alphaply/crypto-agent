import json
import pytest
from backend import storage

from backend.utils.protection_scan import (initialize_schema, active_plan_page,
                                           activity_scan_complete, migrate_activity_page, recent_completed_page)


def database():
    return storage.connect()


@pytest.fixture(autouse=True)
def postgres_storage(financial_pg):
    yield


def test_done_history_never_enters_active_page_and_updates_are_atomic():
    with database() as conn:
        initialize_schema(conn.cursor())
        for index in range(100):
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                         ('old', str(index), 'LONG', '{"state":"DONE"}'))
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                     ('active', 'ETH/USDT:USDT', 'SHORT', '{"state":"WAITING"}'))
        statements = []
        execute = conn.execute
        def traced(statement, *args, **kwargs):
            statements.append(str(statement))
            return execute(statement, *args, **kwargs)
        conn.execute = traced
        page = active_plan_page(conn, limit=2)
        assert [row['config_id'] for row in page] == ['active']
        assert not any('SELECT payload' in sql for sql in statements)
        conn.execute("UPDATE real_protection_plans SET payload='{\"state\":\"DONE\"}' WHERE config_id='active'")
        assert active_plan_page(conn, limit=2) == []
        # Unknown/malformed evidence must not be silently retired as completed.
        conn.execute("UPDATE real_protection_plans SET payload='not-json' WHERE config_id='active'")
        assert active_plan_page(conn, limit=2)[0]['state'] == 'UNKNOWN'


def test_native_index_rebuild_is_bounded_and_resumes_without_parsing_large_record(monkeypatch):
    monkeypatch.setenv('RUNTIME_SCAN_BATCH_SIZE', '2')
    monkeypatch.setenv('RUNTIME_MAX_RECORD_BYTES', '100')
    with database() as conn:
        for index in range(5):
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                         ('old', str(index), 'LONG', '{"state":"DONE"}'))
        oversized = json.dumps({'state': 'DONE', 'original': 'x' * 1000})
        conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)', ('large', 'BTC', 'LONG', oversized))
        # Exercise the durable rebuild path after existing source records remain.
        conn.execute('DELETE FROM protection_plan_activity')
        conn.execute('UPDATE protection_plan_scan_progress SET cursor=0,complete=0 WHERE id=1')
        initialize_schema(conn.cursor())
        migrate_activity_page(conn.cursor())
        assert conn.execute('SELECT count(*) FROM protection_plan_activity').fetchone()[0] == 2
        assert not activity_scan_complete(conn)
        assert active_plan_page(conn, limit=2) == []
        assert not activity_scan_complete(conn)
        page = active_plan_page(conn, limit=2)
        assert page == []
        assert conn.execute("SELECT state,payload_bytes FROM protection_plan_activity WHERE config_id='large'").fetchone() == ('DONE', len(oversized.encode('utf-8')))
        assert conn.execute("SELECT payload FROM real_protection_plans WHERE config_id='large'").fetchone()[0] == oversized
        assert migrate_activity_page(conn.cursor())
        assert activity_scan_complete(conn)
        conn.execute('ANALYZE protection_plan_activity')
        conn.execute('SET LOCAL enable_seqscan = off')
        plan = conn.execute("EXPLAIN SELECT plan_rowid FROM protection_plan_activity WHERE state!='DONE' AND plan_rowid>%s ORDER BY plan_rowid LIMIT %s", (0, 2)).fetchall()
        assert any('idx_protection_active_page' in row[0] for row in plan), [row[0] for row in plan]


def test_recent_done_accounting_uses_metadata_without_returning_done_to_protection_poll():
    with database() as conn:
        initialize_schema(conn.cursor())
        for index in range(5):
            conn.execute('INSERT INTO real_protection_plans(config_id,symbol,side,payload) VALUES(%s,%s,%s,%s)',
                         (str(index), 'ETH', 'LONG', json.dumps({'state': 'DONE', 'updated_at': 100 + index})))
        assert active_plan_page(conn, limit=2) == []
        first = recent_completed_page(conn, since=99, limit=2)
        second = recent_completed_page(conn, since=99, after_cursor=(first[-1]['updated_at'], first[-1]['plan_rowid']), limit=2)
        assert [row['config_id'] for row in first + second] == ['0', '1', '2', '3']
        assert all(set(row.keys()) == {'config_id', 'plan_rowid', 'updated_at'} for row in first)
