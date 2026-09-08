from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.deps import get_current_user
from backend.app.schemas.payloads import PricingDeleteRequest, PricingSaveRequest, UpdatePositionProtectionRequest
from backend.app.services.stats_service import (
    delete_pricing_payload,
    get_agent_stats_payload,
    get_equity_compare_payload,
    get_financial_stats_payload,
    get_kline_payload,
    get_position_stats_payload,
    get_token_stats_payload,
    list_pricing_payload,
    save_pricing_payload,
    update_position_protection_payload,
)


router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/tokens")
def token_stats(_: dict = Depends(get_current_user)):
    return {"success": True, **get_token_stats_payload()}


@router.get("/pricing")
def list_pricing(_: dict = Depends(get_current_user)):
    return {"success": True, **list_pricing_payload()}


@router.post("/pricing")
def save_pricing(payload: PricingSaveRequest, _: dict = Depends(get_current_user)):
    return {
        "success": True,
        **save_pricing_payload(payload.model, payload.input_price, payload.output_price, payload.currency),
    }


@router.delete("/pricing")
def delete_pricing(payload: PricingDeleteRequest, _: dict = Depends(get_current_user)):
    try:
        data = delete_pricing_payload(payload.model)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/financial")
def financial(symbol: str = "BTC/USDT", _: dict = Depends(get_current_user)):
    return {"success": True, **get_financial_stats_payload(symbol)}


@router.get("/agent/{config_id}")
def agent_stats(config_id: str, _: dict = Depends(get_current_user)):
    return {"success": True, **get_agent_stats_payload(config_id)}


@router.get("/position/{config_id}")
def position_stats(config_id: str, _: dict = Depends(get_current_user)):
    try:
        data = get_position_stats_payload(config_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.post("/position/protection")
def update_position_protection(
    payload: UpdatePositionProtectionRequest,
    _: dict = Depends(get_current_user),
):
    try:
        data = update_position_protection_payload(
            config_id=payload.config_id,
            symbol=payload.symbol,
            side=payload.side,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
            clear_stop_loss=payload.clear_stop_loss,
            clear_take_profit=payload.clear_take_profit,
            expected_revision=payload.expected_revision,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        from backend.utils.position_protection import ConcurrentProtectionUpdate
        if isinstance(exc, ConcurrentProtectionUpdate):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail=f"Failed to update position protection: {exc}") from exc
    return {"success": not bool(data.get('error')) and data.get('state') in {'ACTIVE', 'WAITING'}, **data}



@router.get("/equity-compare")
def equity_compare(symbol: str = "BTC/USDT", config_ids: str = "", _: dict = Depends(get_current_user)):
    return {"success": True, **get_equity_compare_payload(symbol, config_ids)}


@router.get("/kline/{config_id}")
def kline(config_id: str, timeframe: str = "1h", _: dict = Depends(get_current_user)):
    try:
        data = get_kline_payload(config_id, timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}
