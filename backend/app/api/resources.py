"""Authenticated, read-only resource diagnostics; never a model tool."""
from fastapi import APIRouter, Depends

from backend.app.core.deps import get_current_user
from backend.utils.resource_diagnostics import resource_snapshot

router = APIRouter(prefix='/api/resources', tags=['resources'],
                   dependencies=[Depends(get_current_user)])


@router.get('/snapshot')
def snapshot():
    return resource_snapshot()
