from fastapi import APIRouter, Depends

from backend.app.core.deps import get_current_user
from backend.app.schemas.payloads import CleanHistoryRequest, GenerateDailySummaryRequest, UpdateDailySummaryRequest
from backend.app.services.dashboard_service import (
    build_history_payload,
    clean_history_payload,
    generate_daily_summary_payload,
    get_daily_summaries_payload,
    get_orders_payload,
    update_daily_summary_payload,
)


router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def history(symbol: str = "BTC/USDT", config_id: str = "ALL", page: int = 1, compare_ids: str = "", _: dict = Depends(get_current_user)):
    compare = [item.strip() for item in compare_ids.split(",") if item.strip()]
    payload = build_history_payload(symbol=symbol, agent_filter=config_id, page=page, compare_ids=compare)
    return {"success": True, **payload}


@router.get("/orders")
def orders(config_id: str, page: int = 1, per_page: int = 20, _: dict = Depends(get_current_user)):
    return {"success": True, **get_orders_payload(config_id, page, per_page)}


@router.get("/daily-summaries")
def daily_summaries(config_id: str, days: int = 7, _: dict = Depends(get_current_user)):
    return {"success": True, **get_daily_summaries_payload(config_id, days)}


@router.post("/daily-summaries/generate")
def generate_daily_summary(payload: GenerateDailySummaryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **generate_daily_summary_payload(payload.config_id, payload.date)}


@router.put("/daily-summaries")
def update_daily_summary(payload: UpdateDailySummaryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **update_daily_summary_payload(payload.date, payload.config_id, payload.summary)}


@router.post("/clean")
def clean_history(payload: CleanHistoryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **clean_history_payload(payload.symbol)}
