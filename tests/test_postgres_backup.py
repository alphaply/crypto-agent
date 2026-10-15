import asyncio
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from backend.storage import backup
from backend.app.services import database_export


@pytest.mark.parametrize('outcome', ['success', 'error', 'timeout'])
def test_dump_credentials_stay_out_of_arguments_and_failure_removes_file(tmp_path, monkeypatch, outcome):
    address = 'postgresql://user:p%40ss%3A%25@db:5433/test?sslmode=require&keepalives=1'
    monkeypatch.setenv('DATABASE_URL', address)
    monkeypatch.setattr(backup.shutil, 'which', lambda _: 'pg_dump')
    output = tmp_path / 'backup.dump'
    def run(arguments, *, env, capture_output, timeout, check):
        assert address not in repr(arguments)
        assert env['PGDATABASE'] == 'test'
        assert env['PGUSER'] == 'user' and env['PGPASSWORD'] == 'p@ss:%'
        assert env['PGHOST'] == 'db' and env['PGPORT'] == '5433'
        assert env['PGSSLMODE'] == 'require'
        assert Path(env['PGSERVICEFILE']).read_text() == '[crypto_backup]\nkeepalives=1\n'
        assert 'p@ss:%' not in repr(arguments)
        assert '--format=custom' in arguments
        output.write_bytes(b'PGDMP-partial')
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(arguments, timeout)
        return SimpleNamespace(returncode=int(outcome == 'error'))
    monkeypatch.setattr(backup.subprocess, 'run', run)
    if outcome == 'success':
        assert backup.dump_database(output) == output
        assert output.read_bytes().startswith(b'PGDMP')
    else:
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)) as error:
            backup.dump_database(output)
        assert address not in str(error.value)
        assert not output.exists()


@pytest.mark.parametrize('disconnect', [False, True])
def test_response_removes_temporary_dump_after_download_or_disconnect(monkeypatch, disconnect):
    monkeypatch.setattr(database_export, 'dump_database', lambda path: Path(path).write_bytes(b'PGDMP-test'))
    response = database_export.database_file_response()
    path = Path(response.path)
    assert path.exists()
    messages = []
    async def send(message):
        messages.append(message)
        if disconnect and message['type'] == 'http.response.body':
            raise ConnectionError('client disconnected')
    async def receive():
        return {'type':'http.disconnect'}
    async def execute():
        await response({'type':'http','method':'GET','headers':[]}, receive, send)
    if disconnect:
        with pytest.raises(ConnectionError):
            asyncio.run(execute())
    else:
        asyncio.run(execute())
        assert messages[-1]['body'] == b'PGDMP-test'
    assert not path.exists()


def test_response_removes_temp_file_when_dump_fails(monkeypatch):
    paths = []
    def failed(path):
        paths.append(Path(path))
        raise RuntimeError('failed')
    monkeypatch.setattr(database_export, 'dump_database', failed)
    with pytest.raises(RuntimeError):
        database_export.database_file_response()
    assert paths and not paths[0].exists()
