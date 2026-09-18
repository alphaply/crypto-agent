"""Bounded-memory, consistent SQLite exports, cleaned up after transmission."""
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from backend import database


def database_file_response() -> FileResponse:
    fd, path = tempfile.mkstemp(prefix="crypto-export-", suffix=".db")
    os.close(fd)
    deadline = time.monotonic() + 90

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Database busy; retry export shortly")

    try:
        with closing(sqlite3.connect(database.DB_NAME, timeout=10)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target, pages=1024, progress=progress, sleep=0.05)
        filename = f"trading_data_{datetime.now(database.TZ_CN):%Y%m%d_%H%M%S}.db"
        return FileResponse(
            path, filename=filename, media_type="application/x-sqlite3",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(os.unlink, path),
        )
    except BaseException:
        os.unlink(path)
        raise
