import json
import uuid

from langchain_core.messages import HumanMessage

from backend.agent.chat_graph import (
    delete_chat_threads,
    get_chat_interrupt,
    get_chat_state,
    stream_chat,
    stream_resume_chat,
)
from backend.config import config as global_config
from backend.database import (
    create_chat_session,
    delete_chat_session,
    delete_chat_sessions,
    get_chat_session,
    get_chat_sessions,
    touch_chat_session,
    update_chat_session_title,
)
from backend.utils.llm_utils import build_chat_openai, invoke_with_retry

from backend.app.services.common import logger, serialize_message


def chat_bootstrap_payload():
    configs = []
    for cfg in global_config.get_all_symbol_configs():
        configs.append(
            {
                "config_id": cfg.get("config_id", ""),
                "symbol": cfg.get("symbol", ""),
                "model": cfg.get("model", ""),
                "mode": cfg.get("mode", "STRATEGY"),
                "title": cfg.get("title"),
            }
        )
    return {"configs": configs, "sessions": get_chat_sessions(limit=200)}


def create_chat_session_payload(config_id: str, title: str | None = None):
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise FileNotFoundError("Config not found")

    session_id = uuid.uuid4().hex
    symbol = cfg.get("symbol", "")
    default_title = cfg.get("title") or f"{symbol} | {cfg.get('mode', 'STRATEGY')}"
    create_chat_session(session_id, config_id, symbol, title or default_title)
    return {"session_id": session_id}


def get_chat_session_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    state = get_chat_state(session_id, config_id=session["config_id"])
    messages = state.get("messages", []) if state else []
    return {
        "session": dict(session),
        "messages": [serialize_message(msg) for msg in messages],
        "pending_approval": get_chat_interrupt(session_id, config_id=session["config_id"]),
    }


def get_chat_messages_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")
    state = get_chat_state(session_id, config_id=session["config_id"])
    messages = state.get("messages", []) if state else []
    return {"session": dict(session), "messages": [serialize_message(msg) for msg in messages]}


def stream_chat_events(session_id: str, user_input: str | None = None, approval: str | None = None):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    try:
        has_error = False
        if approval is not None:
            generator = stream_resume_chat(session_id, approval == "true", config_id=session["config_id"])
        else:
            payload = {"q": user_input, "config_id": session["config_id"]}
            generator = stream_chat(session_id, payload)

        for event in generator:
            if isinstance(event, dict):
                if event.get("type") == "error":
                    has_error = True
                yield event
            else:
                yield {"type": "token", "token": event}

        if not has_error:
            touch_chat_session(session_id)
            state = get_chat_state(session_id, config_id=session["config_id"])
            final_messages = [serialize_message(msg) for msg in state.get("messages", [])]
            yield {
                "type": "done",
                "messages": final_messages,
                "pending_approval": get_chat_interrupt(session_id, config_id=session["config_id"]),
            }
    except Exception as exc:
        logger.error(f"Chat stream error: {exc}")
        yield {"type": "error", "message": str(exc)}


def summarize_chat_title_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    state = get_chat_state(session_id, config_id=session["config_id"])
    messages = state.get("messages", []) if state else []
    if not messages:
        raise ValueError("No messages in the session")

    content_to_summarize = ""
    for msg in messages[:3]:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        content_to_summarize += f"{role}: {str(msg.content)[:200]}\n"

    cfg = global_config.get_config_by_id(session["config_id"])
    llm = build_chat_openai(
        model=cfg.get("model"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("api_base"),
        temperature=0,
    )
    summary_prompt = (
        "Summarize the following conversation into a very short title in Chinese within 6 characters, "
        "without punctuation.\n\n"
        f"{content_to_summarize}"
    )
    try:
        response = invoke_with_retry(
            lambda: llm.invoke([HumanMessage(content=summary_prompt)]),
            logger=logger,
            context=f"title-summary session={session_id} config_id={session['config_id']} model={cfg.get('model')}",
        )
        new_title = str(response.content).strip().replace('"', "").replace("'", "")
    except Exception as exc:
        logger.warning(f"Title summary failed: {exc}")
        new_title = "新会话"

    new_title = new_title[:10] if len(new_title) > 10 else new_title
    update_chat_session_title(session_id, new_title)
    return {"title": new_title}


def clear_chat_messages_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")
    delete_chat_threads([session_id])
    touch_chat_session(session_id)
    return {"message": "Chat thread cleared."}


def delete_chat_session_payload(session_id: str):
    deleted = delete_chat_session(session_id)
    delete_chat_threads([session_id])
    return {"deleted": deleted}


def delete_chat_sessions_payload(session_ids: list[str]):
    deleted = delete_chat_sessions(session_ids)
    delete_chat_threads(session_ids)
    return {"deleted": deleted}
