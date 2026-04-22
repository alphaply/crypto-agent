from fastapi import APIRouter, Depends, HTTPException

from backend.app.core.deps import get_current_user
from backend.app.core.security import authenticate_password, create_access_token, get_expected_password
from backend.app.schemas.payloads import LoginRequest


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(payload: LoginRequest):
    if not get_expected_password():
        raise HTTPException(status_code=500, detail="Server password is not configured")
    if not authenticate_password(payload.password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    token = create_access_token("admin")
    return {"success": True, "token": token, "user": {"name": "admin"}}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"success": True, "user": user}
