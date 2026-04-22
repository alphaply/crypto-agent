from fastapi import APIRouter

from backend.app.services.common import list_symbols


router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/bootstrap")
def bootstrap():
    symbols = list_symbols()
    return {"success": True, "symbols": symbols, "current_symbol": symbols[0] if symbols else "BTC/USDT"}
