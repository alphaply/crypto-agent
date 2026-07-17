import json
import re
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from backend.agent.chat_graph import (
    TEMPORARY_CHAT_TIMEFRAMES,
    compact_chat_memory,
    conversation_memory_payload,
    delete_chat_threads,
    get_chat_interrupt,
    get_chat_state,
    stream_chat,
    stream_resume_chat,
)
from backend.config import config as global_config
from backend.config_store import load_effective_runtime_snapshot, load_management_snapshot
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
from backend.utils.market_data import MarketTool

from backend.app.services.common import logger, serialize_message


SUPPORTED_MARKETS = {
    "binance": ("spot", "swap"),
    "okx": ("spot", "swap"),
}
MARKET_SYMBOL_CACHE_TTL_SECONDS = 15 * 60
_market_symbol_cache: dict[tuple[str, str], tuple[float, list[dict[str, str]]]] = {}
PERSISTENCE_ERROR_MESSAGE = "回答已生成，但暂时无法保存到会话历史。"
r"""
LEGACY_MODEL_SIGNATURE_RE = re.compile(
    r"\n{2}---\s*\n>\s*(?:🧠\s*)?(?:本次回答由.*?完成\。?|This response was completed by.*?\.?)(?:\s*)$",
    re.IGNORECASE | re.DOTALL,
)
"""


def _parse_runtime(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return dict(raw_value)
    if not raw_value:
        return {}
    try:
        payload = json.loads(str(raw_value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _serialize_session(session: dict[str, Any]) -> dict[str, Any]:
    payload = dict(session)
    payload["session_type"] = str(payload.get("session_type") or "task")
    payload["runtime"] = _parse_runtime(payload.pop("runtime_json", "{}"))
    return payload


def _serialize_chat_messages(messages: list[Any]) -> list[dict[str, Any]]:
    payloads = []
    for message in messages:
        payload = serialize_message(message)
        if payload.get("role") == "assistant" and isinstance(payload.get("content"), str):
            content = payload["content"]
            prefix, marker, footer = content.rpartition("\n\n---\n>")
            if marker and len(footer.strip()) <= 180 and "**" in footer:
                content = prefix
            payload["content"] = content.rstrip()
        payloads.append(payload)
    return payloads


def _state_contains_model_completion(state: dict[str, Any] | None, completion: dict[str, Any] | None) -> bool:
    """Confirm that the streamed model result is present in the persisted checkpoint."""
    if not state or not completion:
        return False

    last_assistant = next(
        (message for message in reversed(state.get("messages") or []) if isinstance(message, AIMessage)),
        None,
    )
    if last_assistant is None:
        return False

    payload = serialize_message(last_assistant)
    persisted_content = str(payload.get("content") or "")
    persisted_reasoning = str(payload.get("reasoning_content") or "")
    completion_content = str(completion.get("content") or "")
    completion_reasoning = str(completion.get("reasoning_content") or "")
    completion_has_tools = bool(completion.get("has_tool_calls"))

    return (
        persisted_content == completion_content
        and persisted_reasoning == completion_reasoning
        and bool(payload.get("tool_calls")) == completion_has_tools
    )


def _management_chat_options() -> dict[str, list[dict[str, Any]]]:
    snapshot = load_management_snapshot()
    providers = []
    for provider in snapshot.get("llm_providers", []):
        secret = (provider.get("secrets") or {}).get("api_key") or {}
        providers.append(
            {
                "provider_id": provider.get("provider_id", ""),
                "name": provider.get("name", ""),
                "model": provider.get("model", ""),
                "api_base": provider.get("api_base", ""),
                "thinking_enabled": provider.get("thinking_enabled"),
                "reasoning_effort": provider.get("reasoning_effort", ""),
                "system_prompt_role": provider.get("system_prompt_role", "system"),
                "api_key_configured": bool(secret.get("configured")),
                "api_key_masked": secret.get("masked_value", ""),
            }
        )

    profiles = []
    for profile in snapshot.get("exchange_profiles", []):
        exchange = str(profile.get("exchange") or "").lower()
        if exchange not in SUPPORTED_MARKETS:
            continue
        secrets = profile.get("secrets") or {}
        api_key = secrets.get("api_key") or {}
        secret = secrets.get("secret") or {}
        passphrase = secrets.get("passphrase") or {}
        profiles.append(
            {
                "profile_id": profile.get("profile_id", ""),
                "name": profile.get("name", ""),
                "exchange": exchange,
                "market_type": profile.get("market_type", "swap"),
                "supported_market_types": list(SUPPORTED_MARKETS[exchange]),
                "configured": bool(api_key.get("configured") and secret.get("configured")),
                "api_key_masked": api_key.get("masked_value", ""),
                "passphrase_configured": bool(passphrase.get("configured")),
            }
        )
    return {"llm_providers": providers, "exchange_profiles": profiles}


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
                "thinking_enabled": cfg.get("thinking_enabled"),
                "reasoning_effort": cfg.get("reasoning_effort") or "",
            }
        )
    options = _management_chat_options()
    return {
        "configs": configs,
        "sessions": [_serialize_session(item) for item in get_chat_sessions(limit=200)],
        "llm_providers": options["llm_providers"],
        "exchange_profiles": options["exchange_profiles"],
        "market_capabilities": [
            {"exchange": exchange, "market_types": list(market_types)}
            for exchange, market_types in SUPPORTED_MARKETS.items()
        ],
        "temporary_chat": {
            "timeframes": list(TEMPORARY_CHAT_TIMEFRAMES),
            "includes_task_history": False,
            "refreshes_news": True,
        },
    }


def _runtime_snapshot() -> dict[str, Any]:
    return load_effective_runtime_snapshot()


def _resolve_temporary_runtime(runtime: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    requested = dict(runtime or {})
    exchange_profile_id = str(requested.get("exchange_profile_id") or "").strip()
    provider_id = str(requested.get("llm_provider_id") or "").strip()
    symbol = str(requested.get("symbol") or "").strip().upper()
    market_type = str(requested.get("market_type") or "spot").lower()
    global_requirement = str(requested.get("global_requirement") or "").strip()
    requested_prompt_role = str(requested.get("system_prompt_role") or "").strip().lower()

    if not exchange_profile_id or not provider_id or not symbol:
        raise ValueError("Temporary chat requires an exchange profile, symbol, and LLM provider")
    if market_type not in {"spot", "swap"}:
        raise ValueError("Unsupported market type")
    if len(global_requirement) > 4000:
        raise ValueError("Temporary chat global requirement is too long")

    snapshot = _runtime_snapshot()
    profile = next(
        (item for item in snapshot.get("exchange_profiles", []) if item.get("profile_id") == exchange_profile_id),
        None,
    )
    provider = next(
        (item for item in snapshot.get("llm_providers", []) if item.get("provider_id") == provider_id),
        None,
    )
    if not profile:
        raise FileNotFoundError("Exchange profile not found")
    if not provider:
        raise FileNotFoundError("LLM provider not found")

    exchange = str(profile.get("exchange") or "").lower()
    if exchange not in SUPPORTED_MARKETS or market_type not in SUPPORTED_MARKETS[exchange]:
        raise ValueError("The selected exchange does not support this market type")
    if not profile.get("api_key") or not profile.get("secret"):
        raise ValueError("The selected exchange profile is missing API credentials")
    if exchange == "okx" and not profile.get("passphrase"):
        raise ValueError("The selected OKX profile is missing its passphrase")
    if not provider.get("api_key"):
        raise ValueError("The selected LLM provider is missing an API key")
    stored_runtime = {
        "exchange_profile_id": exchange_profile_id,
        "exchange": exchange,
        "market_type": market_type,
        "symbol": symbol,
        "llm_provider_id": provider_id,
        "provider_name": provider.get("name") or provider_id,
        "model": provider.get("model") or "",
        "global_requirement": global_requirement,
        "thinking_enabled": provider.get("thinking_enabled"),
        "reasoning_effort": provider.get("reasoning_effort") or "",
        "system_prompt_role": requested_prompt_role if requested_prompt_role in {"system", "user"} else provider.get("system_prompt_role", "system"),
        "read_only": True,
    }
    effective_runtime = {
        **stored_runtime,
        "api_key": provider.get("api_key"),
        "api_base": provider.get("api_base"),
        "extra_body": provider.get("extra_body") or {},
        "exchange_profile": {
            "profile_id": exchange_profile_id,
            "exchange": exchange,
            "api_key": profile.get("api_key"),
            "secret": profile.get("secret"),
            "passphrase": profile.get("passphrase"),
        },
    }
    return stored_runtime, effective_runtime


def _effective_session_runtime(session: dict[str, Any]) -> dict[str, Any] | None:
    if str(session.get("session_type") or "task") != "temporary":
        return None
    stored_runtime, _ = _resolve_temporary_runtime(_parse_runtime(session.get("runtime_json")))
    return stored_runtime


def create_chat_session_payload(
    config_id: str | None = None,
    title: str | None = None,
    mode: str = "task",
    runtime: dict[str, Any] | None = None,
):
    session_mode = str(mode or "task").lower()
    session_id = uuid.uuid4().hex
    if session_mode == "temporary":
        stored_runtime, _ = _resolve_temporary_runtime(runtime or {})
        symbol = stored_runtime["symbol"]
        default_title = f"{symbol} | Temporary chat"
        create_chat_session(
            session_id,
            "",
            symbol,
            title or default_title,
            session_type="temporary",
            runtime_json=json.dumps(stored_runtime, ensure_ascii=False),
        )
        return {"session_id": session_id, "session": _serialize_session(get_chat_session(session_id) or {})}

    cfg = global_config.get_config_by_id(str(config_id or ""))
    if not cfg:
        raise FileNotFoundError("Config not found")

    symbol = cfg.get("symbol", "")
    default_title = cfg.get("title") or f"{symbol} | {cfg.get('mode', 'STRATEGY')}"
    create_chat_session(session_id, cfg["config_id"], symbol, title or default_title)
    return {"session_id": session_id, "session": _serialize_session(get_chat_session(session_id) or {})}


def list_market_symbols_payload(exchange_profile_id: str, market_type: str, keyword: str = "") -> dict[str, Any]:
    profile_id = str(exchange_profile_id or "").strip()
    requested_market = str(market_type or "spot").lower()
    if not profile_id:
        raise ValueError("Exchange profile is required")
    snapshot = _runtime_snapshot()
    profile = next((item for item in snapshot.get("exchange_profiles", []) if item.get("profile_id") == profile_id), None)
    if not profile:
        raise FileNotFoundError("Exchange profile not found")
    exchange = str(profile.get("exchange") or "").lower()
    if exchange not in SUPPORTED_MARKETS or requested_market not in SUPPORTED_MARKETS[exchange]:
        raise ValueError("Unsupported exchange market")

    cache_key = (profile_id, requested_market)
    cached = _market_symbol_cache.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < MARKET_SYMBOL_CACHE_TTL_SECONDS:
        catalog = cached[1]
    else:
        catalog = MarketTool(
            exchange_profile=profile,
            market_type=requested_market,
        ).list_symbols(requested_market)
        _market_symbol_cache[cache_key] = (now, catalog)

    needle = str(keyword or "").strip().upper()
    if needle:
        catalog = [item for item in catalog if needle in item["symbol"].upper() or needle in item["base"].upper()]
    return {
        "exchange_profile_id": profile_id,
        "exchange": exchange,
        "market_type": requested_market,
        "symbols": catalog[:100],
    }


def get_chat_session_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    runtime = _effective_session_runtime(session)
    state = get_chat_state(session_id, config_id=session["config_id"], runtime=runtime)
    messages = state.get("messages", []) if state else []
    return {
        "session": _serialize_session(session),
        "messages": _serialize_chat_messages(messages),
        "conversation_memory": conversation_memory_payload(state),
        "pending_approval": get_chat_interrupt(session_id, config_id=session["config_id"], runtime=runtime),
    }


def get_chat_messages_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")
    runtime = _effective_session_runtime(session)
    state = get_chat_state(session_id, config_id=session["config_id"], runtime=runtime)
    messages = state.get("messages", []) if state else []
    return {
        "session": _serialize_session(session),
        "messages": _serialize_chat_messages(messages),
        "conversation_memory": conversation_memory_payload(state),
    }


def get_chat_memory_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")
    runtime = _effective_session_runtime(session)
    state = get_chat_state(session_id, config_id=session["config_id"], runtime=runtime)
    return {"session": _serialize_session(session), "conversation_memory": conversation_memory_payload(state)}


def compact_chat_memory_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")
    runtime = _effective_session_runtime(session)
    memory = compact_chat_memory(session_id, config_id=session["config_id"], runtime=runtime)
    return {"session": _serialize_session(session), "conversation_memory": memory}


def stream_chat_events(
    session_id: str,
    user_input: str | None = None,
    approval: str | None = None,
    retry: bool = False,
    replace_last: bool = False,
):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    model_completion = None
    try:
        runtime = _effective_session_runtime(session)
        has_error = False
        persistence_error = None
        if approval is not None:
            generator = stream_resume_chat(session_id, approval == "true", config_id=session["config_id"], runtime=runtime)
        else:
            payload = {
                "q": user_input,
                "config_id": session["config_id"],
                "retry_last": bool(retry),
                "replace_last_user_message": bool(retry and replace_last),
            }
            generator = stream_chat(session_id, payload, runtime=runtime)

        for event in generator:
            if isinstance(event, dict):
                if event.get("type") == "model_complete":
                    model_completion = event
                    continue
                if event.get("type") == "error":
                    if event.get("phase") == "persistence" and model_completion:
                        persistence_error = event.get("message") or PERSISTENCE_ERROR_MESSAGE
                        logger.error(
                            "Chat response completed but post-generation processing failed: session=%s code=%s cause=%s",
                            session_id,
                            event.get("error_code"),
                            event.get("cause_code") or event.get("error_code"),
                        )
                        continue
                    has_error = True
                yield event
            else:
                yield {"type": "token", "token": event}

        if not has_error:
            state = None
            final_messages = None
            pending_approval = None
            conversation_memory = None
            state_loaded = False
            try:
                state = get_chat_state(session_id, config_id=session["config_id"], runtime=runtime)
                state_loaded = True
                final_messages = _serialize_chat_messages(state.get("messages", []))
            except Exception:
                logger.exception("Chat checkpoint verification failed after model completion: session=%s", session_id)

            completion_persisted = bool(
                state_loaded
                and (model_completion is None or _state_contains_model_completion(state, model_completion))
            )
            if model_completion and completion_persisted and persistence_error:
                logger.warning(
                    "Recovered post-generation chat error because the completed response is present in the checkpoint: session=%s",
                    session_id,
                )
                persistence_error = None
            elif not completion_persisted:
                persistence_error = persistence_error or PERSISTENCE_ERROR_MESSAGE
                # Keep the streamed draft visible in the browser. Replacing it with an older
                # checkpoint would make a successfully generated answer disappear.
                final_messages = None

            if completion_persisted:
                try:
                    touch_chat_session(session_id)
                except Exception:
                    # The checkpoint is the source of truth for chat history. A stale session
                    # timestamp must not be reported to the user as a lost answer.
                    logger.exception("Chat session timestamp update failed: session=%s", session_id)

                try:
                    pending_approval = get_chat_interrupt(
                        session_id,
                        config_id=session["config_id"],
                        runtime=runtime,
                    )
                except Exception:
                    logger.exception("Chat interrupt readback failed: session=%s", session_id)

                try:
                    conversation_memory = conversation_memory_payload(state)
                except Exception:
                    # Rolling-memory metadata is optional response decoration and does not
                    # change whether the assistant message itself was checkpointed.
                    logger.exception("Chat memory payload assembly failed: session=%s", session_id)

            if pending_approval:
                yield {"type": "approval_required", "approval": pending_approval}
            yield {
                "type": "done",
                "messages": final_messages,
                "conversation_memory": conversation_memory,
                "pending_approval": pending_approval,
                "persisted": completion_persisted,
                "persistence_error": persistence_error,
            }
    except Exception as exc:
        logger.exception("Chat stream finalization error: session=%s", session_id)
        if model_completion:
            yield {
                "type": "done",
                "messages": None,
                "conversation_memory": None,
                "pending_approval": None,
                "persisted": False,
                "persistence_error": PERSISTENCE_ERROR_MESSAGE,
            }
        else:
            yield {
                "type": "error",
                "message": "聊天请求处理失败，请重试。",
                "phase": "finalization",
                "error_code": "unknown_error",
                "retryable": False,
                "partial_content": False,
            }


def _session_llm_config(session: dict[str, Any]) -> dict[str, Any]:
    runtime = _effective_session_runtime(session)
    if runtime:
        _, effective_runtime = _resolve_temporary_runtime(runtime)
        return effective_runtime
    cfg = global_config.get_config_by_id(session["config_id"])
    if not cfg:
        raise FileNotFoundError("Config not found")
    return cfg


def summarize_chat_title_payload(session_id: str):
    session = get_chat_session(session_id)
    if not session:
        raise FileNotFoundError("Chat session not found")

    runtime = _effective_session_runtime(session)
    state = get_chat_state(session_id, config_id=session["config_id"], runtime=runtime)
    messages = state.get("messages", []) if state else []
    if not messages:
        raise ValueError("No messages in the session")

    content_to_summarize = ""
    for msg in messages[:3]:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        content_to_summarize += f"{role}: {str(msg.content)[:200]}\n"

    cfg = _session_llm_config(session)
    llm = build_chat_openai(
        model=cfg.get("model"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("api_base"),
        temperature=0,
        extra_body=cfg.get("extra_body"),
        thinking_enabled=cfg.get("thinking_enabled"),
        reasoning_effort=cfg.get("reasoning_effort"),
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
            context=f"title-summary session={session_id} model={cfg.get('model')}",
        )
        new_title = str(response.content).strip().replace('"', "").replace("'", "")
    except Exception as exc:
        logger.warning(f"Title summary failed: {exc}")
        new_title = "New chat"

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
