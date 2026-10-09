from fastapi import APIRouter, HTTPException

from backend.app.services.public_service import (
    build_public_compare_payload,
    build_public_dashboard_payload,
    build_public_history_payload,
    build_public_daily_summaries_payload,
    build_public_short_memories_payload,
    build_public_usage_payload,
    build_public_workspace_payload,
)


router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/polymarket")
def polymarket():
    from backend.app.services.news_service import get_latest_news

    news = get_latest_news()
    predictions = news.get('polymarket') or {'enabled': False, 'events': [], 'source_health': {}}
    return {"success": True, **predictions, 'snapshot_id': news.get('snapshot_id'),
            'as_of': news.get('as_of'), 'stale': news.get('stale', False),
            'status': news.get('status')}


@router.get("/dashboard")
def dashboard(symbol: str | None = None):
    return {"success": True, **build_public_dashboard_payload(symbol)}


@router.get("/compare")
def compare(symbol: str | None = None, config_ids: str = ""):
    return {"success": True, **build_public_compare_payload(symbol, config_ids)}


@router.get("/history")
def history(symbol: str = "BTC/USDT", config_id: str = "ALL", page: int = 1, compare_ids: str = ""):
    compare = [item.strip() for item in compare_ids.split(",") if item.strip()]
    return {"success": True, **build_public_history_payload(symbol, config_id, page, compare)}


@router.get("/daily-summaries")
def daily_summaries(config_id: str = "ALL", symbol: str | None = None, days: int | None = 7, limit: int = 200):
    return {
        "success": True,
        **build_public_daily_summaries_payload(symbol=symbol, config_id=config_id, days=days, limit=limit),
    }


@router.get("/short-memories")
def short_memories(config_id: str = "ALL", symbol: str | None = None, limit: int = 200):
    return {
        "success": True,
        **build_public_short_memories_payload(symbol=symbol, config_id=config_id, limit=limit),
    }


@router.get("/workspace/{config_id}")
def workspace(config_id: str, timeframe: str = "1h", symbol: str | None = None):
    try:
        payload = build_public_workspace_payload(config_id, timeframe, symbol=symbol) if symbol else build_public_workspace_payload(config_id, timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **payload}


@router.get("/usage")
def usage():
    return {"success": True, **build_public_usage_payload()}
