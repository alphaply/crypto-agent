from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.core.deps import get_current_user
from backend.app.services import pricing_service
from backend.database_news import read_state

router = APIRouter(prefix='/api/pricing', tags=['pricing'], dependencies=[Depends(get_current_user)])


@router.get('/catalog')
def catalog(q: str = Query('', max_length=120), limit: int = Query(100, ge=1, le=500)):
    try:
        return {'success': True, **pricing_service.catalog_payload(q, limit)}
    except Exception as exc:
        raise HTTPException(502, 'Model catalog is unavailable') from exc


@router.get('/sync/status')
def status():
    return {'success': True, **read_state('pricing')}


@router.post('/sync')
def sync():
    return {'success': True, **pricing_service.sync_prices()}
