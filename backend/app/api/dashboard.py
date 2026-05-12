from fastapi import APIRouter, Depends

from backend.app.core.deps import get_current_user
from backend.app.services.dashboard_service import build_dashboard_overview


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def overview(symbol: str | None = None, page: int = 1, _: dict = Depends(get_current_user)):
    return {"success": True, **build_dashboard_overview(symbol=symbol, page=page)}
