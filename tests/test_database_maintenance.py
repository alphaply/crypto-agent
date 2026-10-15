import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import database
from backend.app.services.database_service import clean_database, inspect_database, preview_cleanup


@pytest.fixture
def maintenance_db(pg, tmp_path, monkeypatch):
    from backend.app.services import database_service as service
    monkeypatch.setattr(service, 'DATA_DIR', tmp_path)
    def dump(path):
        Path(path).write_bytes(b'test-dump')
        return path
    from backend.storage import backup
    monkeypatch.setattr(backup, 'dump_database', dump)
    with database.get_db_conn() as conn:
        conn.cursor().executemany('INSERT INTO summaries(id,timestamp,config_id,content) VALUES(%s,%s,%s,%s)', [
            (1, '2020-01-01 00:00:00', 'a', 'old'), (2, '2020-01-01 00:00:00', 'b', 'other'),
            (3, '2099-01-01 00:00:00', 'a', 'recent')])
        conn.execute("INSERT INTO execution_fills(account_scope,symbol,trade_id,timestamp_ms,payload) VALUES('account','BTC/USDT','fill',1600000000000,'{}')")
        conn.commit()
    return tmp_path


def test_preview_backup_cleanup_is_scoped_and_preserves_evidence(maintenance_db):
    report = inspect_database()
    assert report['integrity'] == 'connected'
    assert report['reclaimable_bytes'] is None
    preview = preview_cleanup(['summaries'], '2021-01-01', 'a')
    assert preview['counts'] == {'summaries': 1}
    result = clean_database(['summaries'], '2021-01-01', preview['preview_token'], 'a')
    assert result['deleted'] == {'summaries': 1}
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 1
    backup = maintenance_db / 'maintenance-backups' / result['backup']
    assert backup.read_bytes() == b'test-dump'


def test_stale_preview_and_protected_tables_rejected(maintenance_db):
    preview = preview_cleanup(['summaries'], '2021-01-01')
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO summaries(id,timestamp,config_id,content) VALUES(4,'2020-02-01','a','newly imported')")
        conn.commit()
    with pytest.raises(ValueError, match='重新预览'):
        clean_database(['summaries'], '2021-01-01', preview['preview_token'])
    with pytest.raises(ValueError, match='不可清理'):
        preview_cleanup(['execution_fills'], '2021-01-01')
    with pytest.raises(ValueError, match='7 天'):
        preview_cleanup(['summaries'], '2099-01-01')


def test_database_api_requires_auth_and_import_is_read_only(maintenance_db):
    from fastapi.testclient import TestClient
    from backend.app.main import app
    from backend.app.core.deps import get_current_user
    client = TestClient(app)
    assert client.get('/api/database/analysis').status_code == 401
    assert client.post('/api/database/compact').status_code == 401
    assert client.post('/api/database/rebuild-history').status_code == 401
    copy = maintenance_db / 'legacy.sqlite'
    with sqlite3.connect(copy) as conn:
        conn.execute('CREATE TABLE old_records(id INTEGER)')
        conn.execute('INSERT INTO old_records VALUES(1)')
    original = copy.read_bytes()
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'test'}
    try:
        result = client.post('/api/database/analyze-import', content=original, headers={'Content-Type': 'application/octet-stream'})
        assert result.status_code == 200
        assert result.json()['integrity'] == 'ok'
        assert client.post('/api/database/analyze-import', content=b'bad database').status_code == 400
        assert copy.read_bytes() == original
        with database.get_db_conn() as conn:
            assert conn.execute('SELECT count(*) FROM summaries').fetchone()[0] == 3
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_compaction_keeps_all_records(maintenance_db):
    from backend.app.services.database_service import compact_database
    compact_database()
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 3
        assert conn.execute('SELECT 1').fetchone()[0] == 1


def test_backup_failure_prevents_cleanup(maintenance_db, monkeypatch):
    from backend.app.services import database_service as service
    preview = preview_cleanup(['summaries'], '2021-01-01')
    def failed():
        raise RuntimeError('backup failed')
    monkeypatch.setattr(service, '_backup', failed)
    with pytest.raises(RuntimeError, match='backup failed'):
        clean_database(['summaries'], '2021-01-01', preview['preview_token'])
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM summaries').fetchone()[0] == 3


def test_change_during_backup_is_rechecked_before_delete(maintenance_db, monkeypatch):
    from backend.app.services import database_service as service
    preview = preview_cleanup(['summaries'], '2021-01-01')
    def changing_backup():
        with database.get_db_conn() as conn:
            conn.execute("INSERT INTO summaries(id,timestamp,config_id,content) VALUES(4,'2020-01-01','a','raced')")
            conn.commit()
        return 'saved.dump'
    monkeypatch.setattr(service, '_backup', changing_backup)
    with pytest.raises(ValueError, match='重新预览'):
        clean_database(['summaries'], '2021-01-01', preview['preview_token'])
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT count(*) FROM summaries').fetchone()[0] == 4


def test_same_id_update_during_backup_is_rejected_and_new_content_preserved(maintenance_db, monkeypatch):
    from backend.app.services import database_service as service

    preview = preview_cleanup(['summaries'], '2021-01-01', 'a')
    backed_up = {}

    def changing_backup():
        with database.get_db_conn() as conn:
            backed_up.update(dict(conn.execute('SELECT * FROM summaries WHERE id=1').fetchone()))
        # Model a separately committed edit after pg_dump took its snapshot.
        with database.get_db_conn() as conn:
            conn.execute("UPDATE summaries SET content='edited after backup snapshot' WHERE id=1")
            conn.commit()
        return 'saved.dump'

    monkeypatch.setattr(service, '_backup', changing_backup)
    with pytest.raises(ValueError, match='重新预览'):
        clean_database(['summaries'], '2021-01-01', preview['preview_token'], 'a')

    with database.get_db_conn() as conn:
        current = dict(conn.execute('SELECT * FROM summaries WHERE id=1').fetchone())
        assert current['id'] == backed_up['id']
        assert current['timestamp'] == backed_up['timestamp']
        assert current['content'] == 'edited after backup snapshot'
        assert backed_up['content'] == 'old'
        assert conn.execute('SELECT count(*) FROM summaries').fetchone()[0] == 3
        assert conn.execute('SELECT count(*) FROM database_maintenance_log').fetchone()[0] == 0
    refreshed = preview_cleanup(['summaries'], '2021-01-01', 'a')
    assert refreshed['counts'] == preview['counts']
    assert refreshed['preview_token'] != preview['preview_token']
