"""Principal-scoped live HTTP evidence pages, without a callable MCP tool."""
from functools import partial
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials

from backend.utils.trade_audit import live_audit
from .paper_api import bearer


router = APIRouter(prefix='/api/mcp/operations',tags=['mcp-evidence'])


async def live_caller(credentials:HTTPAuthorizationCredentials=Depends(bearer)):
    from .auth import AuthorizationProvider
    from .server import active_public_url
    from .settings import settings
    if not credentials:
        raise HTTPException(401,'Live bearer authorization required')
    token = await AuthorizationProvider(active_public_url() or settings()['public_url']).verify_token(credentials.credentials)
    if token is None or (token.claims or {}).get('key_type') == 'paper':
        raise HTTPException(401,'Valid live bearer authorization required')
    return {'id':token.client_id,'key_type':'live','scopes':token.scopes,
            'profile_ids':(token.claims or {}).get('profile_ids',[])}


@router.get('/{profile_id}/{operation_id}/audit')
async def audit(profile_id:str,operation_id:str,view:Literal['original','latest']='original',
                offset:int=Query(0,ge=0),length:int=Query(2048,ge=1,le=2048),version:str | None=None,
                caller:dict=Depends(live_caller)):
    from .resilience import run
    try:
        return await run('read',partial(live_audit,caller,profile_id,operation_id,view=view,offset=offset,length=length,version=version),
                         label='live_audit_http')
    except FileNotFoundError as exc:
        raise HTTPException(404,str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
