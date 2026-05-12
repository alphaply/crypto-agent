from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.app.core.deps import get_current_user
from backend.app.schemas.payloads import (
    CleanHistoryRequest,
    DeleteDailySummaryRequest,
    GenerateDailySummaryRequest,
    GenerateShortMemoryRequest,
    UpdateShortMemoryRequest,
    UpdateDailySummaryRequest,
)
from backend.app.services.dashboard_service import (
    build_history_payload,
    clean_history_payload,
    generate_daily_summary_payload,
    generate_short_memory_payload,
    get_daily_summaries_payload,
    get_short_memories_payload,
    list_short_memories_payload,
    get_orders_payload,
    export_daily_summaries_payload,
    list_daily_summaries_payload,
    delete_daily_summary_payload,
    update_daily_summary_payload,
    update_short_memory_payload,
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
def daily_summaries(
    config_id: str = "ALL",
    symbol: str | None = None,
    days: int | None = 7,
    limit: int = 200,
    _: dict = Depends(get_current_user),
):
    if config_id and config_id != "ALL" and not symbol:
        return {"success": True, **get_daily_summaries_payload(config_id, days or 7)}
    return {
        "success": True,
        **list_daily_summaries_payload(symbol=symbol, config_id=config_id, days=days, limit=limit),
    }


@router.get("/daily-summaries/export")
def export_daily_summaries(
    config_id: str = "ALL",
    symbol: str | None = None,
    days: int | None = None,
    _: dict = Depends(get_current_user),
):
    content = export_daily_summaries_payload(symbol=symbol, config_id=config_id, days=days)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="daily_summaries.txt"'},
    )


@router.post("/daily-summaries/generate")
def generate_daily_summary(payload: GenerateDailySummaryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **generate_daily_summary_payload(payload.config_id, payload.date)}


@router.get("/short-memories")
def short_memories(
    config_id: str = "ALL",
    symbol: str | None = None,
    limit: int = 200,
    _: dict = Depends(get_current_user),
):
    if config_id and config_id != "ALL" and not symbol:
        return {"success": True, **get_short_memories_payload(config_id, limit=limit)}
    return {"success": True, **list_short_memories_payload(symbol=symbol, config_id=config_id, limit=limit)}


@router.post("/short-memories/generate")
def generate_short_memory(payload: GenerateShortMemoryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **generate_short_memory_payload(payload.config_id, payload.bucket_start)}


@router.put("/short-memories")
def update_short_memory(payload: UpdateShortMemoryRequest, _: dict = Depends(get_current_user)):
    try:
        return {
            "success": True,
            **update_short_memory_payload(
                payload.config_id,
                payload.bucket_start,
                payload.market_summary,
                payload.position_summary,
            ),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/daily-summaries")
def update_daily_summary(payload: UpdateDailySummaryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **update_daily_summary_payload(payload.date, payload.config_id, payload.summary)}


@router.delete("/daily-summaries")
def delete_daily_summary(payload: DeleteDailySummaryRequest, _: dict = Depends(get_current_user)):
    try:
        return {"success": True, **delete_daily_summary_payload(payload.date, payload.config_id)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/clean")
def clean_history(payload: CleanHistoryRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **clean_history_payload(payload.symbol)}
