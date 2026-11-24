"""Resolve discovery credentials without persisting or changing runtime configuration."""
from datetime import datetime, timezone
import time

from langchain_core.messages import HumanMessage
from starlette.concurrency import run_in_threadpool

from backend.app.schemas.provider_models import ProviderDiscoveryRequest, ProviderTestRequest
from backend.config_store import load_effective_runtime_snapshot
from backend.utils.llm_utils import (
    build_chat_model,
    extract_message_text,
    extract_reasoning_content,
    extract_usage,
    resolve_compatibility_mode,
)
from backend.utils.log_safety import redact_log_text, register_log_secrets
from backend.utils.provider_catalog import CatalogError, connection_identity, fetch_models


def resolve_discovery_connection(request: ProviderDiscoveryRequest) -> dict:
    connection = request.model_dump(exclude={"credential"})
    identity = connection_identity(connection)
    connection["api_base"] = identity[0]
    credential = request.credential
    if credential.mode == "draft":
        key = credential.api_key.get_secret_value()
    elif credential.mode == "saved":
        saved = next((item for item in load_effective_runtime_snapshot().get("llm_providers", [])
                      if item.get("provider_id") == credential.provider_id), None)
        if saved is None:
            raise CatalogError("provider_not_found", 404)
        if connection_identity(saved) != identity:
            raise CatalogError("connection_changed", 400)
        key = saved.get("api_key") or ""
    else:
        key = ""
    if not key.strip():
        raise CatalogError("missing_credentials", 400)
    return {**connection, "api_key": key}


async def discover_provider_models(request: ProviderDiscoveryRequest) -> dict:
    connection = await run_in_threadpool(resolve_discovery_connection, request)
    return await fetch_models(connection)


def _sanitize_test_error(exc: BaseException, api_key: str) -> str:
    text = redact_log_text(str(exc) or type(exc).__name__)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = text.strip()
    if len(text) > 400:
        text = text[:397] + "..."
    prefix = type(exc).__name__
    return text if text.startswith(prefix) else f"{prefix}: {text}"


def _run_provider_test(connection: dict, request: ProviderTestRequest) -> dict:
    register_log_secrets(connection["api_key"])
    started = time.perf_counter()
    tested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    resolved_mode = (
        "decisions"
        if request.api_protocol == "decisions"
        else resolve_compatibility_mode(
            request.compatibility_mode,
            model=request.model,
            base_url=connection["api_base"],
        )
    )
    try:
        if request.api_protocol == "decisions":
            from backend.utils import jev

            result = jev.score_news(
                {
                    "title": "Bitcoin ETF daily net inflow reaches $500M",
                    "content": request.prompt,
                    "source": "model_test",
                    "category": "crypto",
                },
                {**connection, "model": request.model},
                instructions="Score market relevance.",
                criteria=["0: irrelevant", "100: highly relevant"],
            )
            score_val = result.get("score")
            confidence = result.get("confidence")
            preview = f"score={score_val}" + (f" · confidence={confidence}" if confidence is not None else "")
            return {
                "success": True,
                "ok": True,
                "model": request.model,
                "api_protocol": request.api_protocol,
                "compatibility_mode": resolved_mode,
                "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "reply_preview": preview[:300],
                "reasoning_preview": "",
                "usage": {},
                "error": "",
                "tested_at": tested_at,
            }

        llm = build_chat_model(
            model=request.model,
            api_key=connection["api_key"],
            base_url=connection["api_base"],
            temperature=request.temperature,
            streaming=False,
            extra_body=request.extra_body or None,
            thinking_enabled=request.thinking_enabled,
            reasoning_effort=request.reasoning_effort or None,
            compatibility_mode=request.compatibility_mode,
            timeout_seconds=25.0,
        )
        response = llm.invoke([HumanMessage(content=request.prompt)])
        reply = extract_message_text(response).strip() or str(getattr(response, "content", "") or "").strip()
        reasoning = (extract_reasoning_content(response) or "").strip()
        raw_usage = extract_usage(response) or {}
        usage = {
            k: v
            for k, v in raw_usage.items()
            if isinstance(v, (int, float, str)) and not isinstance(v, bool)
        }
        return {
            "success": True,
            "ok": True,
            "model": request.model,
            "api_protocol": request.api_protocol,
            "compatibility_mode": resolved_mode,
            "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "reply_preview": reply[:300],
            "reasoning_preview": reasoning[:200],
            "usage": usage,
            "error": "",
            "tested_at": tested_at,
        }
    except Exception as exc:
        return {
            "success": True,
            "ok": False,
            "model": request.model,
            "api_protocol": request.api_protocol,
            "compatibility_mode": resolved_mode,
            "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "reply_preview": "",
            "reasoning_preview": "",
            "usage": {},
            "error": _sanitize_test_error(exc, connection["api_key"]),
            "tested_at": tested_at,
        }


async def test_provider_model(request: ProviderTestRequest) -> dict:
    discovery_request = ProviderDiscoveryRequest(
        api_base=request.api_base,
        api_protocol=request.api_protocol,
        compatibility_mode=request.compatibility_mode,
        decisions_api=request.decisions_api,
        credential=request.credential,
    )
    connection = await run_in_threadpool(resolve_discovery_connection, discovery_request)
    return await run_in_threadpool(_run_provider_test, connection, request)
