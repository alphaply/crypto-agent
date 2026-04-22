from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from backend.app.core.deps import get_current_user
from backend.app.schemas.payloads import PromptDeleteRequest, PromptSaveRequest, SaveConfigRequest
from backend.app.services.config_service import (
    BLOCKED_PROMPT_FILES,
    delete_config_payload,
    delete_prompt_payload,
    export_config_payload,
    get_config_dependencies_payload,
    get_raw_config_payload,
    list_prompts_payload,
    read_prompt_payload,
    save_config_payload,
    save_prompt_payload,
)


router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config(_: dict = Depends(get_current_user)):
    return {"success": True, **get_raw_config_payload()}


@router.put("")
def save_config(payload: SaveConfigRequest, _: dict = Depends(get_current_user)):
    if payload.configs is None:
        raise HTTPException(status_code=400, detail="Configs cannot be empty")
    return {"success": True, **save_config_payload(payload.configs, payload.global_settings)}


@router.get("/export")
def export_config(_: dict = Depends(get_current_user)):
    content, filename = export_config_payload()
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=content, media_type="application/json", headers=headers)


@router.get("/prompts")
def list_prompts(_: dict = Depends(get_current_user)):
    return {"success": True, **list_prompts_payload()}


@router.get("/prompts/content")
def read_prompt(name: str = Query(...), _: dict = Depends(get_current_user)):
    if not name or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if name in BLOCKED_PROMPT_FILES:
        raise HTTPException(status_code=403, detail="Prompt is blocked")
    return {"success": True, **read_prompt_payload(name)}


@router.put("/prompts")
def save_prompt(payload: PromptSaveRequest, _: dict = Depends(get_current_user)):
    if not payload.name or ".." in payload.name or not payload.name.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Invalid prompt filename")
    if payload.name in BLOCKED_PROMPT_FILES:
        raise HTTPException(status_code=403, detail="Prompt is blocked")
    return {"success": True, **save_prompt_payload(payload.name, payload.content)}


@router.delete("/prompts")
def delete_prompt(payload: PromptDeleteRequest, _: dict = Depends(get_current_user)):
    if payload.name in BLOCKED_PROMPT_FILES:
        raise HTTPException(status_code=403, detail="Prompt is blocked")
    if not payload.name or payload.name in ["real.txt", "strategy.txt"]:
        raise HTTPException(status_code=400, detail="Protected prompt file")
    return {"success": True, **delete_prompt_payload(payload.name)}


@router.get("/{config_id}/dependencies")
def config_dependencies(config_id: str, _: dict = Depends(get_current_user)):
    try:
        payload = get_config_dependencies_payload(config_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **payload}


@router.delete("/{config_id}")
def delete_config(config_id: str, _: dict = Depends(get_current_user)):
    try:
        payload = delete_config_payload(config_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **payload}
