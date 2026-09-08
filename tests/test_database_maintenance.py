import sqlite3
from unittest.mock import patch

import pytest

from backend import database
from backend.app.services.database_service import clean_database, inspect_database, preview_cleanup


@pytest.fixture
def maintenance_db(tmp_path):
    path = tmp_path / 'state.db'
    with patch.object(database, 'DB_NAME', str(path)):
        with database.get_db_conn() as conn:
            conn.execute('CREATE TABLE summaries (id INTEGER PRIMARY KEY, timestamp TEXT, config_id TEXT, content TEXT)')
            conn.executemany('INSERT INTO summaries VALUES(?,?,?,?)', [
                (1, '2020-01-01 00:00:00', 'a', 'old'), (2, '2020-01-01 00:00:00', 'b', 'other'),
                (3, '2099-01-01 00:00:00', 'a', 'recent')])
            conn.execute('CREATE TABLE execution_fills (id INTEGER)')
            conn.execute('INSERT INTO execution_fills VALUES(1)')
            conn.commit()
        yield path


def test_preview_backup_cleanup_is_scoped_and_preserves_evidence(maintenance_db):
    report = inspect_database()
    assert report['integrity'] == 'ok'
    preview = preview_cleanup(['summaries'], '2021-01-01', 'a')
    assert preview['counts'] == {'summaries': 1}
    result = clean_database(['summaries'], '2021-01-01', preview['preview_token'], 'a')
    assert result['deleted'] == {'summaries': 1}
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM execution_fills').fetchone()[0] == 1
    backup = maintenance_db.parent / 'maintenance-backups' / result['backup']
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 3
    finally:
        conn.close()


def test_stale_preview_and_protected_tables_rejected(maintenance_db):
    preview = preview_cleanup(['summaries'], '2021-01-01')
    with database.get_db_conn() as conn:
        conn.execute("INSERT INTO summaries VALUES(4,'2020-02-01','a','newly imported')")
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
    original = maintenance_db.read_bytes()
    app.dependency_overrides[get_current_user] = lambda: {'sub': 'test'}
    try:
        result = client.post('/api/database/analyze-import', content=original, headers={'Content-Type': 'application/octet-stream'})
        assert result.status_code == 200
        assert result.json()['integrity'] == 'ok'
        assert client.post('/api/database/analyze-import', content=b'bad database').status_code == 400
        assert maintenance_db.read_bytes() == original
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_compaction_keeps_all_records(maintenance_db):
    from backend.app.services.database_service import compact_database
    compact_database()
    with database.get_db_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 3
        assert conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
