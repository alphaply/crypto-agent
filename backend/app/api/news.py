from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.core.deps import get_current_user
from backend.app.schemas.news import default_news_settings, default_news_sources
from backend.app.services.news_service import get_latest_news, get_news_status, refresh_news
from backend import database_news

router = APIRouter(prefix='/api/news', tags=['news'])


@router.get('')
def latest():
    return {'success': True, **get_latest_news()}


@router.get('/status')
def status(_: dict = Depends(get_current_user)):
    return {'success': True, **get_news_status()}


@router.get('/runs')
def processing_runs(limit: int = Query(20, ge=1, le=50), _: dict = Depends(get_current_user)):
    return {'success': True, **database_news.processing_runs(limit)}


@router.get('/runs/{run_id}')
def processing_run(run_id: str, _: dict = Depends(get_current_user)):
    run = database_news.processing_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='News processing run not found')
    return {'success': True, 'run': run}


@router.get('/scorer-models')
def scorer_models(provider_id: str = Query(min_length=1), _: dict = Depends(get_current_user)):
    from backend.app.services.news_service import _runtime, _provider, NewsPipelineError
    from backend.utils.jev import JevError, list_models
    try:
        return {'success': True, 'models': list_models(_provider(_runtime(), provider_id, 'Jev scoring'))}
    except (JevError, NewsPipelineError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.post('/refresh')
def refresh(_: dict = Depends(get_current_user)):
    return {'success': True, **refresh_news()}


@router.get('/sources')
def source_defaults(_: dict = Depends(get_current_user)):
    return {'success': True, 'sources': default_news_sources(), 'defaults': default_news_settings()}
