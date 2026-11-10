"""Administrator-only system logs; no arbitrary filesystem paths."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.core.deps import get_current_user
from backend.app.services.log_service import LogCursorExpired, log_status, read_logs
from backend.utils.log_safety import redact_log_text

router = APIRouter(prefix='/api/logs', tags=['logs'], dependencies=[Depends(get_current_user)])


@router.get('')
def logs(limit: int = Query(100, ge=1, le=500),
         min_level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO',
         query: str = Query('', max_length=200), cursor: str | None = Query(None, max_length=512)):
    try:
        return {'success': True, **read_logs(limit=limit, min_level=min_level, query=query, cursor=cursor)}
    except LogCursorExpired as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError as exc:
        raise HTTPException(503, f'日志文件读取失败 ({type(exc).__name__}): {redact_log_text(exc)}') from None


@router.get('/status')
def status():
    try:
        return {'success': True, **log_status()}
    except OSError as exc:
        raise HTTPException(503, f'日志文件状态读取失败 ({type(exc).__name__}): {redact_log_text(exc)}') from None
