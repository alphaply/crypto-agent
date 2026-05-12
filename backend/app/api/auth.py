from fastapi import APIRouter, Depends, HTTPException, Request

from backend.app.core.deps import get_current_user
from backend.app.core.security import (
    authenticate_password,
    check_login_allowed,
    create_access_token,
    get_expected_password,
    record_login_failure,
    reset_login_attempts,
)
from backend.app.schemas.payloads import LoginRequest


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login")
def login(payload: LoginRequest, request: Request):
    ip = _get_client_ip(request)
    allowed, reason = check_login_allowed(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    if not get_expected_password():
        raise HTTPException(status_code=500, detail="Server password is not configured")

    if not authenticate_password(payload.password):
        record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Incorrect password")

    reset_login_attempts(ip)
    token = create_access_token("admin")
    return {"success": True, "token": token, "user": {"name": "admin"}}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"success": True, "user": user}
