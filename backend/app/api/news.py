from fastapi import APIRouter, Depends

from backend.app.core.deps import get_current_user
from backend.app.schemas.news import default_news_settings, default_news_sources
from backend.app.services.news_service import get_latest_news, get_news_status, refresh_news

router = APIRouter(prefix='/api/news', tags=['news'])


@router.get('')
def latest():
    return {'success': True, **get_latest_news()}


@router.get('/status')
def status(_: dict = Depends(get_current_user)):
    return {'success': True, **get_news_status()}


@router.post('/refresh')
def refresh(_: dict = Depends(get_current_user)):
    return {'success': True, **refresh_news()}


@router.get('/sources')
def source_defaults(_: dict = Depends(get_current_user)):
    return {'success': True, 'sources': default_news_sources(), 'defaults': default_news_settings()}
