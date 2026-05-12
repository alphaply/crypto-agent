import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.app.core.deps import get_current_user
from backend.app.schemas.payloads import BulkDeleteSessionsRequest, CreateSessionRequest
from backend.app.services.chat_service import (
    chat_bootstrap_payload,
    clear_chat_messages_payload,
    create_chat_session_payload,
    delete_chat_session_payload,
    delete_chat_sessions_payload,
    get_chat_messages_payload,
    get_chat_session_payload,
    stream_chat_events,
    summarize_chat_title_payload,
)


router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.get("/bootstrap")
def bootstrap(_: dict = Depends(get_current_user)):
    return {"success": True, **chat_bootstrap_payload()}


@router.post("/sessions")
def create_session(payload: CreateSessionRequest, _: dict = Depends(get_current_user)):
    try:
        data = create_chat_session_payload(payload.config_id, payload.title)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, _: dict = Depends(get_current_user)):
    try:
        data = get_chat_session_payload(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **data}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, _: dict = Depends(get_current_user)):
    return {"success": True, **delete_chat_session_payload(session_id)}


@router.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, _: dict = Depends(get_current_user)):
    try:
        data = get_chat_messages_payload(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **data}


@router.get("/sessions/{session_id}/stream")
def stream(session_id: str, message: str | None = None, approval: str | None = None, _: dict = Depends(get_current_user)):
    def event_stream():
        try:
            for event in stream_chat_events(session_id, user_input=message, approval=approval):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except FileNotFoundError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Chat session not found'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/summarize-title")
def summarize_title(session_id: str, _: dict = Depends(get_current_user)):
    try:
        data = summarize_chat_title_payload(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, **data}


@router.post("/sessions/{session_id}/clear")
def clear_session(session_id: str, _: dict = Depends(get_current_user)):
    try:
        data = clear_chat_messages_payload(session_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"success": True, **data}


@router.delete("/sessions")
def delete_sessions(payload: BulkDeleteSessionsRequest, _: dict = Depends(get_current_user)):
    return {"success": True, **delete_chat_sessions_payload(payload.ids)}
