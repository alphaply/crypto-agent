"""QQBot polling endpoints using paper bearer keys rather than administrator JWTs."""
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import paper_competition
from .auth import AuthorizationProvider
from .resilience import run
from .settings import settings

bearer = HTTPBearer(auto_error=False)
router = APIRouter(prefix='/api/paper', tags=['paper-bot'])


async def paper_caller(response: Response, credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    response.headers['Cache-Control'] = 'no-store'
    if not credentials:
        raise HTTPException(401, 'Paper bearer key required')
    from .server import active_public_url
    provider = AuthorizationProvider(active_public_url() or settings()['public_url'])
    token = await provider.verify_token(credentials.credentials)
    if token is None or (token.claims or {}).get('key_type') != 'paper':
        raise HTTPException(401, 'Valid paper bearer key required')
    return {'id': token.client_id, 'key_type': 'paper', 'paper_namespace': token.claims.get('paper_namespace'),
            'scopes': token.scopes, 'profile_ids': []}


@router.get('/events')
async def scan_events(after_id: int = 0, limit: int = 100, user_id: str | None = None, caller: dict = Depends(paper_caller)):
    try:
        return await run('read', partial(paper_competition.scan, caller, after_id, limit, user_id), label='paper_events_http')
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/leaderboard')
async def leaderboard(limit: int = 20, metric: str = 'return_pct', caller: dict = Depends(paper_caller)):
    try:
        return await run('read', partial(paper_competition.leaderboard, caller, limit, metric), label='paper_leaderboard_http')
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400, str(exc)) from exc
