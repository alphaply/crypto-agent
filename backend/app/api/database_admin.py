import sqlite3
from backend.storage import OperationalError
import tempfile
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from backend.app.core.deps import get_current_user
from backend.app.services import database_service as service

router = APIRouter(prefix='/api/database', tags=['database'], dependencies=[Depends(get_current_user)])


class CleanupRequest(BaseModel):
    tables: list[str] = Field(min_length=1, max_length=7)
    before: str
    config_id: str | None = None
    preview_token: str = ''


@router.get('/analysis')
def analysis():
    return service.inspect_database()


@router.get('/cleanup-targets')
def cleanup_targets():
    return service.cleanup_targets()


@router.post('/analyze-import')
async def analyze_import(request: Request):
    # Stream to a temporary file; never replace or execute anything from the uploaded DB.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'import.db'
        size = 0
        async with await anyio.open_file(path, 'wb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise HTTPException(413, '数据库副本最大 512 MiB')
                await stream.write(chunk)
        try:
            return await run_in_threadpool(service.inspect_database, path)
        except (sqlite3.Error, ValueError) as exc:
            raise HTTPException(400, '无法读取有效 SQLite 数据库') from exc


@router.post('/cleanup-preview')
def preview(payload: CleanupRequest):
    try:
        return service.preview_cleanup(payload.tables, payload.before, payload.config_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/cleanup')
def cleanup(payload: CleanupRequest):
    try:
        return service.clean_database(payload.tables, payload.before, payload.preview_token, payload.config_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/compact')
def compact():
    try:
        return service.compact_database()
    except OperationalError as exc:
        raise HTTPException(409, '数据库繁忙，请稍后回收空间') from exc


@router.post('/rebuild-history')
def rebuild_history():
    return service.rebuild_history()
