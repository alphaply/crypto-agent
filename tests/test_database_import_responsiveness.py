import asyncio
import sqlite3
import threading

import anyio
from fastapi import HTTPException
import pytest

from backend.app.api import database_admin


class Upload:
    async def stream(self):
        yield b"first chunk"
        yield b"second chunk"


def test_database_import_analysis_allows_other_async_work(monkeypatch):
    started = threading.Event()
    other_work_finished = threading.Event()

    def inspect(path):
        assert path.read_bytes() == b"first chunksecond chunk"
        started.set()
        # A blocking event-loop analysis cannot allow the peer below to finish.
        assert other_work_finished.wait(2), "Database analysis blocked other requests"
        return {"tables": []}

    monkeypatch.setattr(database_admin.service, "inspect_database", inspect)

    async def run():
        async def unrelated_request():
            while not started.is_set():
                await asyncio.sleep(0.001)
            other_work_finished.set()

        result, _ = await asyncio.gather(
            database_admin.analyze_import(Upload()), unrelated_request())
        return result

    assert asyncio.run(run()) == {"tables": []}


@pytest.mark.parametrize("error", [sqlite3.DatabaseError("invalid"), ValueError("invalid")])
def test_database_import_retains_invalid_upload_response(monkeypatch, error):
    paths = []

    def inspect(path):
        paths.append(path)
        raise error

    monkeypatch.setattr(database_admin.service, "inspect_database", inspect)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(database_admin.analyze_import(Upload()))
    assert failure.value.status_code == 400
    assert not paths[0].exists()


def test_cancelled_request_waits_for_analysis_before_removing_upload(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    paths = []

    def inspect(path):
        paths.append(path)
        started.set()
        assert release.wait(2), "Test did not release analysis"
        assert path.read_bytes() == b"first chunksecond chunk"
        finished.set()
        return {"tables": []}

    monkeypatch.setattr(database_admin.service, "inspect_database", inspect)

    async def run():
        async with anyio.create_task_group() as group:
            group.start_soon(database_admin.analyze_import, Upload())
            while not started.is_set():
                await anyio.sleep(0.001)
            group.cancel_scope.cancel()
            assert paths[0].exists()
            release.set()
        assert finished.is_set()

    anyio.run(run)
    assert not paths[0].exists()
