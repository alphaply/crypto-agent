"""Consistent PostgreSQL exports streamed from a temporary backup file."""
import os
import tempfile
from datetime import datetime, timezone
from fastapi.responses import FileResponse
from backend.storage.backup import dump_database


class TemporaryBackupResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also clean up when a disconnected client cancels/raises in send.
            if os.path.exists(self.path):
                os.unlink(self.path)


def database_file_response() -> FileResponse:
    fd, path = tempfile.mkstemp(prefix='crypto-export-', suffix='.dump')
    os.close(fd)
    try:
        dump_database(path)
        filename = f'trading_data_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.dump'
        return TemporaryBackupResponse(path, filename=filename, media_type='application/octet-stream',
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except BaseException:
        if os.path.exists(path):
            os.unlink(path)
        raise
