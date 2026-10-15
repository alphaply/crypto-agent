"""User-initiated evidence disclosure; deliberately absent from model tools."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.core.deps import get_current_user
from backend.utils.trade_audit import task_audit


router = APIRouter(prefix='/api/trade-receipts', tags=['trade-evidence'])


@router.get('/{config_id}/{operation_id}/audit')
def audit(config_id: str, operation_id: str, view: Literal['original','latest']='original',
          offset: int=Query(0,ge=0),length: int=Query(2048,ge=1,le=2048),version: str | None=None,
          _:dict=Depends(get_current_user)):
    try:
        return task_audit(config_id,operation_id,view=view,offset=offset,length=length,version=version)
    except FileNotFoundError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
