from fastapi import APIRouter, HTTPException

from backend.app.schemas.payloads import SetupApplyRequest
from backend.app.services.setup_service import apply_setup_payload, get_setup_status_payload


router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
def status():
    return {"success": True, **get_setup_status_payload()}


@router.post("/apply")
def apply_setup(payload: SetupApplyRequest):
    try:
        return {"success": True, **apply_setup_payload(payload.model_dump(mode="json"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
