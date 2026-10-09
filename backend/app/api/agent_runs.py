"""Authenticated read-only access to the real agent invocation history."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend.app.core.deps import get_current_user
from backend.database_agent_runs import get_agent_run, list_agent_runs


router = APIRouter(prefix='/api/agent-runs', tags=['agent-runs'], dependencies=[Depends(get_current_user)])


@router.get('')
def list_runs(
    config_id: str | None = Query(None, min_length=1, max_length=200),
    purpose: Literal['decision', 'strategy_summary', 'memory_review', 'daily_summary', 'chat', 'chat_summary', 'news_score', 'news_summary'] | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
):
    return {'success': True, **list_agent_runs(config_id=config_id, purpose=purpose, limit=limit, offset=offset)}


@router.get('/{run_id}')
def run_detail(run_id: str = Path(..., min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$')):
    run = get_agent_run(run_id)
    if run is None:
        raise HTTPException(404, 'Agent run not found')
    return {'success': True, 'run': run}
