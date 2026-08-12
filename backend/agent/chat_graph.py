from __future__ import annotations

import atexit
import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import (
    BaseMessageChunk,
    HumanMessage,
    AIMessage,
    ToolMessage,
    trim_messages,
    message_chunk_to_message,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from backend.agent.agent_graph import start_node as scheduler_start_node
from backend.agent.agent_models import AgentState
from backend.agent.tool_registry import get_trade_tools_for_mode, run_trade_tool
from backend.config import config as global_config
from backend.config_store import load_effective_runtime_snapshot
import backend.database as database
from backend.utils.llm_utils import (
    LLMInvocationError,
    build_chat_model,
    extract_message_text,
    extract_reasoning_content,
    extract_reasoning_token_count,
    format_llm_error_message,
    instruction_message,
    invoke_with_retry,
    sync_langsmith_environment,
)
from backend.utils.logger import setup_logger
from backend.utils.market_data import MarketTool
from backend.storage_paths import data_file
from backend.utils.formatters import (
    format_market_data_to_text,
    format_orders_to_agent_friendly,
    format_positions_to_agent_friendly,
)
from backend.utils.news_context import fetch_news_risk_context

load_dotenv()
logger = setup_logger("ChatGraph")

CHAT_CHECKPOINT_DB = str(data_file("CHAT_CHECKPOINT_DB", "chat_checkpoints.sqlite"))
CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CHAT_MAX_HISTORY_MESSAGES", "12"))
CHAT_TRIM_MAX_TOKENS = int(os.getenv("CHAT_TRIM_MAX_TOKENS", "6000"))
TEMPORARY_CHAT_TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")
CHAT_CONTEXT_RECENT_MESSAGES = int(os.getenv("CHAT_CONTEXT_RECENT_MESSAGES", "10"))
CHAT_CONTEXT_COMPACTION_BATCH = int(os.getenv("CHAT_CONTEXT_COMPACTION_BATCH", "6"))
CHAT_CONTEXT_SUMMARY_MAX_CHARS = int(os.getenv("CHAT_CONTEXT_SUMMARY_MAX_CHARS", "8000"))
CHAT_CONTEXT_MESSAGE_MAX_CHARS = int(os.getenv("CHAT_CONTEXT_MESSAGE_MAX_CHARS", "3000"))
CHAT_CONTEXT_RECENT_MAX_CHARS = int(os.getenv("CHAT_CONTEXT_RECENT_MAX_CHARS", "18000"))


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    symbol: str
    system_prompt: str
    q: str
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    retry_last: bool
    replace_last_user_message: bool
    conversation_summary: str
    conversation_summary_cursor: int
    conversation_summary_updated_at: str

def _get_chat_tools(cfg: Dict[str, Any]):
    if cfg.get("read_only"):
        return []
    return get_trade_tools_for_mode(cfg.get("mode", "STRATEGY"))


def _resolve_temporary_chat_config(runtime: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve temporary-session credentials just-in-time, never from checkpoint state."""
    snapshot = load_effective_runtime_snapshot()
    provider_id = str(runtime.get("llm_provider_id") or "")
    profile_id = str(runtime.get("exchange_profile_id") or "")
    provider = next((item for item in snapshot.get("llm_providers", []) if item.get("provider_id") == provider_id), None)
    profile = next((item for item in snapshot.get("exchange_profiles", []) if item.get("profile_id") == profile_id), None)
    if not provider or not provider.get("api_key"):
        raise ValueError("Temporary chat LLM provider is missing or has no API key")
    if not profile or not profile.get("api_key") or not profile.get("secret"):
        raise ValueError("Temporary chat exchange profile is missing or incomplete")
    if str(profile.get("exchange") or "").lower() == "okx" and not profile.get("passphrase"):
        raise ValueError("Temporary chat OKX profile is missing its passphrase")

    return {
        **runtime,
        "mode": "READ_ONLY",
        "read_only": True,
        "model": provider.get("model") or runtime.get("model") or "",
        "api_key": provider.get("api_key"),
        "api_base": provider.get("api_base") or "",
        "extra_body": provider.get("extra_body") or {},
        "compatibility_mode": provider.get("compatibility_mode") or "auto",
        "thinking_enabled": provider.get("thinking_enabled"),
        "reasoning_effort": provider.get("reasoning_effort") or "",
        "exchange_profile": {
            "profile_id": profile_id,
            "exchange": profile.get("exchange"),
            "api_key": profile.get("api_key"),
            "secret": profile.get("secret"),
            "passphrase": profile.get("passphrase"),
        },
    }


def _resolve_chat_config(configurable: Dict[str, Any]) -> Dict[str, Any]:
    runtime = configurable.get("runtime")
    if isinstance(runtime, dict) and runtime.get("read_only"):
        return _resolve_temporary_chat_config(runtime)
    config_id = configurable.get("config_id")
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError(f"Config not found for config_id={config_id}")
    return cfg


def _temporary_market_context_text(market_data: Dict[str, Any]) -> str:
    """Render the full compact technical context used by task chats, without task history."""
    analysis = market_data.get("analysis") or {}
    if not analysis:
        error = str(market_data.get("error") or "No technical data was returned")
        return f"Technical market data is unavailable for this turn: {error}"
    primary = analysis.get("15m") or next(iter(analysis.values()), {})
    return format_market_data_to_text(
        {
            "current_price": primary.get("price", 0),
            "atr_base": primary.get("atr", 0),
            "sentiment": market_data.get("sentiment") or {},
            "technical_indicators": analysis,
        }
    )


def _temporary_news_context_text(news_context: Dict[str, Any]) -> str:
    headlines = [str(item).strip() for item in (news_context.get("headlines") or []) if str(item).strip()]
    if not headlines:
        if news_context.get("available"):
            return "Risk level: normal\nNo sufficiently relevant macro events or headlines were selected for this turn."
        error = str(news_context.get("error") or "No relevant headlines were retrieved")
        return f"News context is unavailable for this turn: {error}"
    risk_level = str(news_context.get("risk_level") or "unknown")
    source = str(news_context.get("source") or "live news sources")
    reasons = [str(item).strip() for item in (news_context.get("risk_reasons") or []) if str(item).strip()]
    stale_note = " (using a recent cached snapshot)" if news_context.get("stale") else ""
    return "\n".join(
        [
            f"Risk level: {risk_level}{stale_note}",
            *[f"Risk reason: {reason}" for reason in reasons[:3]],
            f"Source: {source}",
            *[f"- {headline}" for headline in headlines],
        ]
    )


def _temporary_account_context_text(account_context: Dict[str, Any]) -> str:
    if not account_context.get("available"):
        return f"Account data is unavailable for this turn: {account_context.get('error') or 'Unknown error'}"

    positions_text = format_positions_to_agent_friendly(account_context.get("real_positions", []))
    orders_text = format_orders_to_agent_friendly(
        [
            {**order, "id": order.get("id") or order.get("order_id")}
            for order in account_context.get("real_open_orders", [])
        ]
    )
    return (
        f"Balance: {account_context.get('balance', 0)}\n"
        f"Available: {account_context.get('available_balance', account_context.get('balance', 0))}\n"
        f"Positions:\n{positions_text}\n\nOpen orders:\n{orders_text}"
    )


def _temporary_analysis_limitations(
    market_context: Dict[str, Any], news_context: Dict[str, Any], account_context: Dict[str, Any]
) -> str:
    limitations = ["Use only the data supplied below. Never invent prices, indicators, news, balances, positions, or orders."]
    if not market_context.get("analysis"):
        limitations.append("Technical analysis is unavailable for this turn; explain that no live technical conclusion can be made.")
    if not news_context.get("available", bool(news_context.get("headlines"))):
        limitations.append("News context is unavailable for this turn; do not infer that there is no news risk.")
    if not account_context.get("available"):
        limitations.append("Account data is unavailable for this turn; do not describe the balance as zero or positions as empty.")
    return "\n".join(f"- {item}" for item in limitations)


def _start_temporary_chat(state: ChatState, configurable: Dict[str, Any], cfg: Dict[str, Any]):
    symbol = str(cfg.get("symbol") or "Chat")
    market_type = str(cfg.get("market_type") or "spot").upper()
    try:
        market_tool = MarketTool(
            exchange_profile=cfg.get("exchange_profile"),
            market_type=cfg.get("market_type"),
            symbol=symbol,
        )
        market_context = market_tool.get_market_analysis(
            symbol,
            mode=market_type,
            timeframes=list(TEMPORARY_CHAT_TIMEFRAMES),
        )
    except Exception as exc:
        logger.warning("Temporary chat context failed for %s: %s", symbol, exc)
        market_context = {"symbol": symbol, "analysis": {}, "error": str(exc)}
    try:
        if "market_tool" not in locals():
            raise RuntimeError("Market data client could not be initialized")
        account_context = market_tool.get_account_status(symbol, is_real=True, agent_name="temporary-chat")
        account_context = {**account_context, "available": not bool(account_context.get("error"))}
    except Exception as exc:
        logger.warning("Temporary chat account context failed for %s: %s", symbol, exc)
        account_context = {"available": False, "error": str(exc)}
    try:
        news_context = fetch_news_risk_context(symbol)
        if not news_context.get("available", bool(news_context.get("headlines"))):
            news_context = {**news_context, "error": "No relevant headlines were retrieved"}
    except Exception as exc:
        logger.warning("Temporary chat news context failed for %s: %s", symbol, exc)
        news_context = {"error": str(exc)}

    global_requirement = str(cfg.get("global_requirement") or "").strip()
    market_context_text = _temporary_market_context_text(market_context)
    news_context_text = _temporary_news_context_text(news_context)
    account_context_text = _temporary_account_context_text(account_context)
    limitations_text = _temporary_analysis_limitations(market_context, news_context, account_context)
    unavailable_history = "Not loaded: temporary chats do not use task short-term memory, daily summaries, or strategy history."
    system_prompt = "\n\n".join(
        part
        for part in (
            "You are a professional crypto market research assistant. Give a clear, evidence-based analysis, separate facts from inference, and state uncertainty and risk plainly.",
            global_requirement and f"## Global requirement\n{global_requirement}",
            (
                "## Temporary read-only session\n"
                f"Exchange: {cfg.get('exchange', '')}\n"
                f"Market: {market_type}\n"
                f"Symbol: {symbol}\n"
                f"Timeframes: {', '.join(TEMPORARY_CHAT_TIMEFRAMES)}\n"
                "This session can analyze only. Do not generate or execute orders, cancellations, transfers, or other trading operations.\n"
                f"{unavailable_history}"
            ),
            f"## Live technical context (refreshed for this message)\n{market_context_text}",
            f"## Live news and macro context (refreshed for this message)\n{news_context_text}",
            f"## Account context (read-only)\n{account_context_text}",
            f"## Analysis limitations\n{limitations_text}",
        )
        if part
    )
    updates = {
        "system_prompt": system_prompt,
        "symbol": symbol,
        "q": state.get("q"),
        "market_context": market_context,
        "account_context": {**account_context, "news_context": news_context},
    }
    if state.get("q") and state.get("retry_last") and state.get("replace_last_user_message"):
        existing_messages = list(state.get("messages") or [])
        last_message = existing_messages[-1] if existing_messages else None
        if isinstance(last_message, HumanMessage):
            updates["messages"] = [HumanMessage(content=state["q"], id=last_message.id)]
    elif state.get("q") and not state.get("retry_last"):
        updates["messages"] = [HumanMessage(content=state["q"])]
    updates["retry_last"] = False
    updates["replace_last_user_message"] = False
    return updates


def _message_counter(msgs: list) -> int:
    return len(msgs)


def _tool_call_ids_from_message(msg) -> list[str]:
    tool_calls = getattr(msg, "tool_calls", None) or []
    ids = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tcid = tc.get("id")
        else:
            tcid = getattr(tc, "id", None)
        if tcid:
            ids.append(str(tcid))
    return ids


def _sanitize_tool_sequences(messages: list):
    sanitized = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            i += 1
            continue
        required_ids = _tool_call_ids_from_message(msg)
        if not required_ids:
            sanitized.append(msg)
            i += 1
            continue
        required = set(required_ids)
        seen = set()
        matched_tools = []
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            tcid = str(getattr(messages[j], "tool_call_id", "") or "")
            if tcid in required and tcid not in seen:
                matched_tools.append(messages[j])
                seen.add(tcid)
            j += 1
        if seen == required:
            sanitized.append(msg)
            sanitized.extend(matched_tools)
        i = j
    return sanitized


def _message_context_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
    else:
        text = str(content or "")
    if text.strip():
        return text
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        return f"Tool calls: {json.dumps(tool_calls, ensure_ascii=False, default=str)}"
    return text


def _message_context_role(message: Any) -> str:
    if isinstance(message, HumanMessage):
        return "User"
    if isinstance(message, ToolMessage):
        return "Tool"
    if isinstance(message, AIMessage):
        return "Assistant"
    return "Message"


def _context_compaction_cutoff(history: list, cursor: int, force: bool = False) -> int:
    cursor = max(0, min(int(cursor or 0), len(history)))
    cutoff = max(cursor, len(history) - CHAT_CONTEXT_RECENT_MESSAGES)
    unsummarized_chars = sum(len(_message_context_text(message)) for message in history[cursor:])
    force_by_size = unsummarized_chars > CHAT_CONTEXT_RECENT_MAX_CHARS
    if cutoff <= cursor and (force or force_by_size) and len(history) - cursor > 1:
        keep_count = min(CHAT_CONTEXT_RECENT_MESSAGES, max(2, len(history) - cursor - 1))
        cutoff = len(history) - keep_count
    if cutoff <= cursor:
        return cursor
    pending_count = cutoff - cursor
    pending_chars = sum(len(_message_context_text(message)) for message in history[cursor:cutoff])
    if (
        not force
        and not force_by_size
        and pending_count < CHAT_CONTEXT_COMPACTION_BATCH
        and pending_chars < CHAT_CONTEXT_SUMMARY_MAX_CHARS
    ):
        return cursor
    return cutoff


def _format_messages_for_compaction(messages: list) -> str:
    chunks = []
    for message in messages:
        text = _message_context_text(message).strip()
        if not text:
            continue
        if len(text) > CHAT_CONTEXT_MESSAGE_MAX_CHARS:
            text = f"{text[:CHAT_CONTEXT_MESSAGE_MAX_CHARS]} …[truncated]"
        chunks.append(f"{_message_context_role(message)}: {text}")
    return "\n\n".join(chunks)


def _compact_chat_context(
    history: list,
    previous_summary: str,
    cursor: int,
    cfg: Dict[str, Any],
    configurable: Dict[str, Any],
    force: bool = False,
) -> tuple[str, int]:
    cutoff = _context_compaction_cutoff(history, cursor, force=force)
    if cutoff <= cursor:
        return previous_summary, cursor

    _emit_stream_status(configurable, "compressing_context", "正在整理较早的对话上下文")
    conversation = _format_messages_for_compaction(history[cursor:cutoff])
    prompt = """Maintain a compact rolling memory for an ongoing chat. The memory is historical context, not live market data.
Keep: user goals and constraints, explicit decisions, assumptions, named entities and values, key analysis conclusions, open questions, and promised follow-ups.
Discard: greetings, repetition, filler, and superseded details. Never invent facts. Write concise Chinese with short headings and bullets.

Existing rolling memory:
{previous_summary}

New conversation to incorporate:
{conversation}""".format(
        previous_summary=(previous_summary or "(none)")[-CHAT_CONTEXT_SUMMARY_MAX_CHARS:],
        conversation=conversation,
    )
    try:
        llm = build_chat_model(
            model=cfg.get("model"),
            api_key=cfg.get("api_key") or os.getenv("OPENAI_API_KEY"),
            base_url=cfg.get("api_base"),
            temperature=0,
            streaming=False,
            extra_body=cfg.get("extra_body"),
            thinking_enabled=cfg.get("thinking_enabled"),
            reasoning_effort=cfg.get("reasoning_effort"),
            compatibility_mode=cfg.get("compatibility_mode"),
        )
        response = invoke_with_retry(
            lambda: llm.invoke([instruction_message(prompt, cfg.get("system_prompt_role"))]),
            logger=logger,
            context=f"chat-context-compaction session={configurable.get('thread_id')} model={cfg.get('model')}",
        )
        summary = _message_context_text(response).strip()
        if not summary:
            raise ValueError("Context compaction returned an empty summary")
        return summary[:CHAT_CONTEXT_SUMMARY_MAX_CHARS], cutoff
    except Exception as exc:
        logger.warning("Chat context compaction failed; retaining recent raw history: %s", exc)
        return previous_summary, cursor


def conversation_memory_payload(state: Dict[str, Any] | None) -> Dict[str, Any]:
    state = state or {}
    messages = state.get("messages") or []
    cursor = max(0, min(int(state.get("conversation_summary_cursor") or 0), len(messages)))
    return {
        "summary": str(state.get("conversation_summary") or ""),
        "summarized_message_count": cursor,
        "recent_message_count": len(messages) - cursor,
        "total_message_count": len(messages),
        "updated_at": state.get("conversation_summary_updated_at") or None,
    }


def compact_chat_memory(session_id: str, config_id: str = None, runtime: Dict[str, Any] | None = None) -> Dict[str, Any]:
    config = _chat_trace_config(session_id, config_id, runtime=runtime)
    snapshot = chat_app.get_state(config)
    state = snapshot.values if snapshot else {}
    history = list(state.get("messages") or [])
    cfg = _resolve_chat_config(config.get("configurable", {}))
    summary = str(state.get("conversation_summary") or "")
    cursor = int(state.get("conversation_summary_cursor") or 0)
    next_summary, next_cursor = _compact_chat_context(history, summary, cursor, cfg, config["configurable"], force=True)
    if next_summary != summary or next_cursor != cursor:
        chat_app.update_state(
            config,
            {
                "conversation_summary": next_summary,
                "conversation_summary_cursor": next_cursor,
                "conversation_summary_updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        snapshot = chat_app.get_state(config)
        state = snapshot.values if snapshot else state
    return conversation_memory_payload(state)


def _trim_chat_messages(
    system_prompt: str,
    history: list,
    conversation_summary: str = "",
    summary_cursor: int = 0,
    system_prompt_role: str = "system",
):
    cursor = max(0, min(int(summary_cursor or 0), len(history)))
    recent_history = history[cursor:]
    memory = str(conversation_summary or "").strip()
    if memory:
        system_prompt = (
            f"{system_prompt}\n\n## Rolling conversation memory\n"
            "This is a compact summary of earlier chat turns. Treat current live context as newer.\n"
            f"{memory}"
        )
    pinned_prompt = instruction_message(system_prompt, system_prompt_role)
    if recent_history:
        token_trimmed_history = trim_messages(
            recent_history,
            max_tokens=CHAT_TRIM_MAX_TOKENS,
            token_counter="approximate",
            strategy="last",
            include_system=False,
            allow_partial=False,
        )
    else:
        token_trimmed_history = []
    token_trimmed_history = _sanitize_tool_sequences(token_trimmed_history)
    count_trimmed_history = trim_messages(
        token_trimmed_history,
        max_tokens=CHAT_MAX_HISTORY_MESSAGES,
        token_counter=_message_counter,
        strategy="last",
        include_system=False,
        allow_partial=False,
    )
    count_trimmed_history = _sanitize_tool_sequences(count_trimmed_history)
    if str(system_prompt_role or "system").lower() == "user" and count_trimmed_history and isinstance(count_trimmed_history[0], HumanMessage):
        first_message = count_trimmed_history[0]
        merged_instruction = HumanMessage(content=f"{system_prompt}\n\n## User request\n{_message_context_text(first_message)}")
        final_messages = [merged_instruction] + count_trimmed_history[1:]
    else:
        final_messages = [pinned_prompt] + count_trimmed_history
    return final_messages


def _emit_stream_status(configurable: Dict[str, Any], stage: str, message: str, attempt: int | None = None):
    event_queue = configurable.get("event_queue")
    if not event_queue:
        return
    payload = {"type": "status", "stage": stage, "message": message}
    if attempt is not None:
        payload["attempt"] = attempt
    event_queue.put(payload)


def _emit_stream_event(configurable: Dict[str, Any], payload: Dict[str, Any]):
    event_queue = configurable.get("event_queue")
    if event_queue:
        event_queue.put(payload)


def _chat_trace_config(session_id: str, config_id: str | None, event_queue=None, runtime: Dict[str, Any] | None = None) -> RunnableConfig:
    sync_langsmith_environment()
    cfg = runtime if runtime else (global_config.get_config_by_id(config_id) if config_id else None)
    symbol = cfg.get("symbol", "Chat") if cfg else "Chat"
    model = cfg.get("model", "Unknown Model") if cfg else "Unknown Model"
    configurable = {"thread_id": session_id, "config_id": config_id}
    if runtime:
        configurable["runtime"] = runtime
    if event_queue is not None:
        configurable["event_queue"] = event_queue
    return {
        "configurable": configurable,
        "metadata": {
            "source": "chat",
            "session_id": session_id,
            "config_id": config_id or "",
            "symbol": symbol,
            "model": model,
        },
        "tags": ["chat", f"config:{config_id or 'unknown'}", f"symbol:{symbol}", f"model:{model}"],
    }


def _chat_error_payload(
    exc: BaseException,
    *,
    phase: str = "generation",
    partial_content: bool = False,
) -> Dict[str, Any]:
    if phase == "persistence":
        cause_code = exc.error_type if isinstance(exc, LLMInvocationError) else "unknown_error"
        return {
            "type": "error",
            "message": format_llm_error_message("persistence_error"),
            "error_code": "persistence_error",
            "cause_code": cause_code,
            "retryable": False,
            "phase": phase,
            "partial_content": partial_content,
        }
    if isinstance(exc, LLMInvocationError):
        return {
            "type": "error",
            "message": str(exc),
            "error_code": exc.error_type,
            "retryable": exc.retryable,
            "phase": phase,
            "partial_content": partial_content,
        }
    return {
        "type": "error",
        "message": format_llm_error_message("unknown_error"),
        "error_code": "unknown_error",
        "retryable": False,
        "phase": phase,
        "partial_content": partial_content,
    }


def start_node(state: ChatState, config: RunnableConfig):
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id")
    q = state.get("q")

    cfg = _resolve_chat_config(configurable)
    if cfg.get("read_only"):
        return _start_temporary_chat(state, configurable, cfg)

    symbol = cfg.get("symbol", "Unknown")
    
    scheduler_state = AgentState(
        symbol=symbol,
        messages=[],
        market_context={},
        account_context={},
        history_context=[],
        full_analysis="",
        human_message=None,
    )
    chat_config = {
        "configurable": {
            **configurable,
            "agent_config": cfg
        }
    }
    # 调用底层 start_node 获取最新数据
    started = scheduler_start_node(scheduler_state, config=chat_config)
    
    system_prompt = started.messages[0].content if started.messages else ""
    
    updates = {
        "system_prompt": system_prompt, 
        "symbol": symbol,
        "q": q,
        "market_context": started.market_context,
        "account_context": started.account_context
    }
    if q and state.get("retry_last") and state.get("replace_last_user_message"):
        existing_messages = list(state.get("messages") or [])
        last_message = existing_messages[-1] if existing_messages else None
        if isinstance(last_message, HumanMessage):
            updates["messages"] = [HumanMessage(content=q, id=last_message.id)]
    elif q and not state.get("retry_last"):
        updates["messages"] = [HumanMessage(content=q)]
    updates["retry_last"] = False
    updates["replace_last_user_message"] = False
    return updates


def model_node(state: ChatState, config: RunnableConfig):
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id")
    cfg = _resolve_chat_config(configurable)

    symbol = cfg.get("symbol", "Chat")
    model_name = cfg.get("model", "Unknown Model")
    api_key = cfg.get("api_key") or os.getenv("OPENAI_API_KEY")
    api_base = cfg.get("api_base")
    
    if not api_key:
        logger.error(f"❌ [Chat Error] No API Key found for {symbol} ({config_id})")
        return {"messages": [AIMessage(content="❌ 错误：未配置 API Key，请在设置中检查。")], "q": None}

    history = list(state.get("messages", []))
    system_prompt = state.get("system_prompt", "")
    conversation_summary = str(state.get("conversation_summary") or "")
    summary_cursor = int(state.get("conversation_summary_cursor") or 0)
    previous_summary = conversation_summary
    previous_cursor = summary_cursor
    conversation_summary, summary_cursor = _compact_chat_context(
        history,
        conversation_summary,
        summary_cursor,
        cfg,
        configurable,
    )
    trimmed = _trim_chat_messages(
        system_prompt,
        history,
        conversation_summary,
        summary_cursor,
        cfg.get("system_prompt_role", "system"),
    )
    
    logger.info(
        f"[Chat] start session={configurable.get('thread_id')} config_id={config_id} "
        f"symbol={symbol} model={model_name} messages={len(trimmed)}"
    )
    _emit_stream_status(configurable, "waiting_model", "正在等待模型响应")

    llm = build_chat_model(
        model=model_name,
        api_key=api_key,
        base_url=api_base,
        temperature=0.5,
        streaming=True,
        extra_body=cfg.get("extra_body"),
        thinking_enabled=cfg.get("thinking_enabled"),
        reasoning_effort=cfg.get("reasoning_effort"),
        compatibility_mode=cfg.get("compatibility_mode"),
    )
    chat_tools = _get_chat_tools(cfg)
    if chat_tools:
        llm = llm.bind_tools(chat_tools)

    stream_state = {"emitted_content": False}

    def _stream_model_response():
        accumulator = _new_stream_accumulator()
        combined_chunk: BaseMessageChunk | None = None
        stream_state["emitted_content"] = False
        trace_config = {"metadata": config.get("metadata", {}), "tags": config.get("tags", [])}
        try:
            for chunk in llm.stream(trimmed, config=trace_config):
                if not isinstance(chunk, BaseMessageChunk):
                    continue

                # LangChain v1 chunks are additive. Keeping the provider-native
                # aggregate preserves Anthropic thinking signatures and OpenAI
                # Responses reasoning/tool blocks for the following graph turn.
                combined_chunk = chunk if combined_chunk is None else combined_chunk + chunk

                reasoning_token = _chunk_reasoning_text(chunk)
                if reasoning_token:
                    accumulator["reasoning_content"] += reasoning_token
                    stream_state["emitted_content"] = True
                    _emit_stream_event(configurable, {"type": "reasoning_token", "token": reasoning_token})

                token = _chunk_to_text(chunk)
                if token:
                    accumulator["content"] += token
                    stream_state["emitted_content"] = True
                    _emit_stream_event(configurable, {"type": "token", "token": token})

                _accumulate_tool_call_chunks(accumulator, chunk)
                _capture_stream_metadata(accumulator, chunk)
                tool_calls = _extract_tool_calls(chunk)
                if tool_calls:
                    stream_state["emitted_content"] = True
                    _emit_stream_event(configurable, {"type": "tool_calls", "tool_calls": tool_calls})
        except Exception as exc:
            if _has_terminal_finish_reason(accumulator):
                logger.warning("Ignoring stream trailer error after terminal finish marker: %r", exc)
            elif stream_state["emitted_content"]:
                raise LLMInvocationError(
                    format_llm_error_message("stream_protocol_error"),
                    error_type="stream_protocol_error",
                    retryable=False,
                    attempts=1,
                    original=exc,
                ) from exc
            else:
                raise

        try:
            response = (
                message_chunk_to_message(combined_chunk)
                if combined_chunk is not None
                else _stream_accumulator_to_message(accumulator, logger=logger)
            )
        except Exception as exc:
            raise LLMInvocationError(
                format_llm_error_message("message_assembly_error"),
                error_type="message_assembly_error",
                retryable=False,
                attempts=1,
                original=exc,
            ) from exc
        _emit_stream_event(
            configurable,
            {
                "type": "model_complete",
                "content": extract_message_text(response),
                "reasoning_content": extract_reasoning_content(response),
                "reasoning_tokens": extract_reasoning_token_count(response),
                "has_tool_calls": bool(response.tool_calls),
            },
        )
        return response

    started_at = time.time()
    response = invoke_with_retry(
        _stream_model_response,
        logger=logger,
        context=f"chat session={configurable.get('thread_id')} config_id={config_id} symbol={symbol} model={model_name}",
        on_retry=lambda next_attempt, total_attempts, error_type, exc: _emit_stream_status(
            configurable,
            "retrying",
            f"模型响应较慢，正在自动重试（第 {next_attempt}/{total_attempts} 次）",
            next_attempt,
        ),
    )
    
    logger.info(
        f"[Chat] success session={configurable.get('thread_id')} config_id={config_id} "
        f"symbol={symbol} model={model_name} duration={time.time() - started_at:.2f}s"
    )

    # 记录 Token 用量（流式响应的最后一个 chunk 含 usage 信息）
    try:
        usage = getattr(response, "response_metadata", {}).get("token_usage", {})
        if not usage:
            # 兼容 usage_metadata 字段（LangChain 0.3+）
            um = getattr(response, "usage_metadata", None) or {}
            if um:
                usage = {
                    "prompt_tokens": um.get("input_tokens", 0),
                    "completion_tokens": um.get("output_tokens", 0),
                }
        if not cfg.get("read_only") and usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            database.save_token_usage(
                symbol=symbol,
                config_id=config_id,
                model=model_name,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
    except Exception as usage_e:
        logger.warning(f"⚠️ [Chat] Failed to save token usage: {usage_e}")

    return {
        "messages": [response], 
        "symbol": symbol, 
        "q": None,
        "retry_last": False,
        "replace_last_user_message": False,
        "conversation_summary": conversation_summary,
        "conversation_summary_cursor": summary_cursor,
        "conversation_summary_updated_at": (
            datetime.now(timezone.utc).isoformat()
            if conversation_summary != previous_summary or summary_cursor != previous_cursor
            else state.get("conversation_summary_updated_at")
        ),
    }


def _run_tool(tool_name: str, args: Dict[str, Any], config_id: str, symbol: str) -> str:
    return run_trade_tool(tool_name, args, config_id, symbol)


def tools_node(state: ChatState, config: RunnableConfig):
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", []) or []
    outputs = []
    
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id")
    
    cfg = _resolve_chat_config(configurable)
    symbol = cfg.get("symbol", "Unknown") if cfg else state.get("symbol", "Unknown")

    for call in tool_calls:
        tool_name = call["name"]
        tool_args = call.get("args", {})

        approval = interrupt(
            {
                "type": "tool_approval",
                "tool_call_id": call["id"],
                "tool_name": tool_name,
                "tool_args": tool_args,
                "config_id": config_id,
                "symbol": symbol,
            }
        )

        approved = False
        if isinstance(approval, dict):
            approved = bool(approval.get("approved"))
        elif isinstance(approval, bool):
            approved = approval

        if not approved:
            outputs.append(ToolMessage(tool_call_id=call["id"], content="Rejected by user."))
            continue

        try:
            result = _run_tool(tool_name, tool_args, config_id, symbol)
            outputs.append(ToolMessage(tool_call_id=call["id"], content=result))
        except Exception as exc:
            logger.error(f"Tool error ({tool_name}): {exc}")
            outputs.append(ToolMessage(tool_call_id=call["id"], content=f"Error: {exc}"))

    return {"messages": outputs, "symbol": symbol}


def should_continue(state: ChatState):
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "end"


workflow = StateGraph(ChatState)
workflow.add_node("start", start_node)
workflow.add_node("model", model_node)
workflow.add_node("tools", tools_node)

workflow.set_entry_point("start")

workflow.add_edge("start", "model")

workflow.add_conditional_edges("model", should_continue, {"tools": "tools", "end": END})
workflow.add_edge("tools", "model")

_checkpointer_cm = SqliteSaver.from_conn_string(CHAT_CHECKPOINT_DB)
checkpointer = _checkpointer_cm.__enter__()
atexit.register(lambda: _checkpointer_cm.__exit__(None, None, None))

chat_app = workflow.compile(checkpointer=checkpointer, name="CryptoChat")


def _chunk_to_text(chunk: BaseMessageChunk | Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            block_type = str(item.get("type") or "").lower()
            if block_type in {"reasoning", "reasoning_content", "thinking"}:
                continue
            parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return ""


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "reasoning_content", "reasoning", "thinking", "delta"):
            if key in value:
                return _coerce_text(value.get(key))
        return ""
    return str(value)


def _chunk_reasoning_text(chunk: BaseMessageChunk | Any) -> str:
    add_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
    resp_meta = getattr(chunk, "response_metadata", {}) or {}

    for container in (add_kwargs, resp_meta):
        for key in ("reasoning_content", "reasoning", "thinking"):
            if key in container:
                reasoning = _coerce_text(container[key])
                if reasoning:
                    return reasoning

    delta = add_kwargs.get("delta") or resp_meta.get("delta") or {}
    if isinstance(delta, dict):
        res = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
        if res:
            return _coerce_text(res)

    content = getattr(chunk, "content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").lower()
            if block_type not in {"reasoning", "reasoning_content", "thinking"}:
                continue
            part = _coerce_text(item)
            if part:
                parts.append(part)
        if parts:
            return "".join(parts)

    return ""


def _new_stream_accumulator() -> dict[str, Any]:
    return {
        "content": "",
        "reasoning_content": "",
        "tool_calls": {},
        "response_metadata": {},
        "usage_metadata": None,
        "message_id": None,
    }


def _tool_chunk_value(chunk: Any, key: str) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(key)
    return getattr(chunk, key, None)


def _normalize_tool_chunk_index(raw_index: Any, fallback: int) -> int:
    if isinstance(raw_index, int):
        return raw_index
    if isinstance(raw_index, str):
        try:
            return int(raw_index)
        except ValueError:
            return fallback
    return fallback


def _accumulate_tool_call_chunks(accumulator: dict[str, Any], chunk: BaseMessageChunk | Any) -> None:
    tool_call_chunks = getattr(chunk, "tool_call_chunks", None) or []
    for fallback_index, raw in enumerate(tool_call_chunks):
        index = _normalize_tool_chunk_index(_tool_chunk_value(raw, "index"), fallback_index)
        current = accumulator["tool_calls"].setdefault(
            index,
            {"index": index, "id": "", "name": "", "args": ""},
        )

        call_id = _tool_chunk_value(raw, "id")
        if call_id and not current["id"]:
            current["id"] = str(call_id)

        name = _tool_chunk_value(raw, "name")
        if name and not current["name"]:
            current["name"] = str(name)

        args = _tool_chunk_value(raw, "args")
        if args is not None:
            current["args"] += str(args)


def _capture_stream_metadata(accumulator: dict[str, Any], chunk: BaseMessageChunk | Any) -> None:
    message_id = getattr(chunk, "id", None)
    if message_id and not accumulator["message_id"]:
        accumulator["message_id"] = message_id

    response_metadata = getattr(chunk, "response_metadata", None) or {}
    if response_metadata:
        accumulator["response_metadata"].update(response_metadata)

    usage_metadata = getattr(chunk, "usage_metadata", None)
    if usage_metadata:
        accumulator["usage_metadata"] = usage_metadata


def _has_terminal_finish_reason(accumulator: dict[str, Any]) -> bool:
    metadata = accumulator.get("response_metadata") or {}
    finish_reason = metadata.get("finish_reason") or metadata.get("stop_reason")
    return bool(str(finish_reason or "").strip())


def _parse_tool_call_args(raw_args: str) -> dict[str, Any]:
    text = str(raw_args or "").strip()
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("tool call args must be a JSON object")
    return parsed


def _assistant_additional_kwargs(accumulator: dict[str, Any]) -> dict[str, Any]:
    reasoning = str(accumulator.get("reasoning_content") or "")
    return {"reasoning_content": reasoning} if reasoning else {}


def _stream_accumulator_to_message(accumulator: dict[str, Any], *, logger) -> AIMessage:
    tool_calls = []
    for index, raw in sorted(accumulator["tool_calls"].items()):
        name = str(raw.get("name") or "").strip()
        if not name:
            logger.warning("Ignoring streamed tool call without name: index=%s raw=%s", index, raw)
            continue

        try:
            args = _parse_tool_call_args(raw.get("args", ""))
        except Exception as exc:
            logger.warning(
                "Invalid streamed tool call args: index=%s name=%s error=%r raw_args=%r",
                index,
                name,
                exc,
                raw.get("args", ""),
            )
            return AIMessage(
                content=(
                    "工具调用参数解析失败，已中止本次工具审批。"
                    "请重新发送请求，或让模型用更简单的参数重试。"
                ),
                additional_kwargs=_assistant_additional_kwargs(accumulator),
                id=accumulator.get("message_id"),
                response_metadata=accumulator.get("response_metadata") or {},
                usage_metadata=accumulator.get("usage_metadata"),
            )

        tool_calls.append(
            {
                "name": name,
                "args": args,
                "id": str(raw.get("id") or f"chat_tool_call_{index}"),
            }
        )

    return AIMessage(
        content="" if tool_calls else accumulator.get("content", ""),
        tool_calls=tool_calls,
        additional_kwargs=_assistant_additional_kwargs(accumulator),
        id=accumulator.get("message_id"),
        response_metadata=accumulator.get("response_metadata") or {},
        usage_metadata=accumulator.get("usage_metadata"),
    )


def _extract_tool_calls(chunk: BaseMessageChunk | Any) -> list:
    """提取增量的工具调用块"""
    tcc = getattr(chunk, "tool_call_chunks", [])
    if not tcc: return []
    
    res = []
    for fallback_index, c in enumerate(tcc):
        call_id = _tool_chunk_value(c, "id")
        name = _tool_chunk_value(c, "name")
        if not call_id and not name:
            continue
        res.append({
            "index": _normalize_tool_chunk_index(_tool_chunk_value(c, "index"), fallback_index),
            "id": call_id,
            "name": name,
        })
    return res


def _stream_worker(run_callable, event_queue):
    try:
        for item in run_callable():
            event_queue.put(item)
    except Exception as exc:
        logger.exception("[Chat Stream] worker failed")
        event_queue.put(exc)
    finally:
        event_queue.put(None)


def _yield_stream_events(run_callable, event_queue, initial_status: str):
    worker = threading.Thread(target=_stream_worker, args=(run_callable, event_queue), daemon=True)

    yield {"type": "status", "stage": "preparing_context", "message": initial_status}
    worker.start()

    model_completed = False
    partial_content = False
    while True:
        item = event_queue.get()
        if item is None:
            break

        if isinstance(item, GraphInterrupt):
            logger.info("[Chat Stream] paused for tool approval interrupt")
            break

        if isinstance(item, BaseException):
            error_type = item.error_type if isinstance(item, LLMInvocationError) else "unknown_error"
            phase = (
                "persistence"
                if model_completed
                else "assembly"
                if error_type == "message_assembly_error"
                else "generation"
            )
            payload = _chat_error_payload(item, phase=phase, partial_content=partial_content)
            logger.error(
                f"[Chat Stream] failed type={payload['error_code']} retryable={payload['retryable']}: {item}"
            )
            yield payload
            break

        if isinstance(item, dict):
            if item.get("type") in {"token", "reasoning_token", "tool_calls"}:
                partial_content = True
            elif item.get("type") == "model_complete":
                model_completed = True
            yield item


def _chat_stream_items(request_payload, config):
    for _ in chat_app.stream(request_payload, config=config, stream_mode="updates"):
        continue


def _resume_stream_items(command, config):
    for item in chat_app.stream(command, config=config, stream_mode="updates"):
        if not isinstance(item, dict):
            continue
        tools_update = item.get("tools")
        if isinstance(tools_update, dict):
            for msg in tools_update.get("messages", []) or []:
                if isinstance(msg, ToolMessage):
                    yield {
                        "type": "tool_result",
                        "tool_call_id": msg.tool_call_id,
                        "content": extract_message_text(msg),
                        "role": "tool",
                    }
        if isinstance(item.get("model"), dict):
            yield {"type": "status", "stage": "waiting_model", "message": "工具已执行，正在等待模型继续分析"}


def stream_chat(session_id: str, payload: Dict[str, Any], runtime: Dict[str, Any] | None = None):
    config_id = payload.pop("config_id", None)
    event_queue = queue.Queue()
    config = _chat_trace_config(session_id, config_id, event_queue, runtime=runtime)

    def run():
        yield from _chat_stream_items(payload, config)

    yield from _yield_stream_events(run, event_queue, "正在整理市场与账户数据")


def stream_resume_chat(session_id: str, approved: bool, config_id: str = None, runtime: Dict[str, Any] | None = None):
    event_queue = queue.Queue()
    config = _chat_trace_config(session_id, config_id, event_queue, runtime=runtime)
    command = Command(resume={"approved": approved})

    def run():
        yield from _resume_stream_items(command, config)

    yield from _yield_stream_events(run, event_queue, "正在继续执行工具审批后的对话")


def invoke_chat(session_id: str, payload: Dict[str, Any], runtime: Dict[str, Any] | None = None):
    config_id = payload.pop("config_id", None)
    config = _chat_trace_config(session_id, config_id, runtime=runtime)
    return chat_app.invoke(payload, config=config)


def resume_chat(session_id: str, approved: bool, config_id: str = None, runtime: Dict[str, Any] | None = None):
    config = _chat_trace_config(session_id, config_id, runtime=runtime)
    return chat_app.invoke(Command(resume={"approved": approved}), config=config)


def get_chat_state(session_id: str, config_id: str = None, runtime: Dict[str, Any] | None = None):
    config = {"configurable": {"thread_id": session_id, "config_id": config_id}}
    snapshot = chat_app.get_state(config)
    return snapshot.values if snapshot else {}


def get_chat_interrupt(session_id: str, config_id: str = None, runtime: Dict[str, Any] | None = None):
    config = {"configurable": {"thread_id": session_id, "config_id": config_id}}
    snapshot = chat_app.get_state(config)
    if not snapshot or not getattr(snapshot, "interrupts", None): return None
    intr = snapshot.interrupts[0]
    return {"id": getattr(intr, "id", ""), "value": getattr(intr, "value", {}) or {}}


def delete_chat_threads(session_ids):
    ids = [sid for sid in session_ids if sid]
    if not ids: return 0
    checkpointer.conn.commit()
    conn = sqlite3.connect(CHAT_CHECKPOINT_DB)
    c = conn.cursor()
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    deleted = 0
    placeholders = ",".join(["?"] * len(ids))
    for (table_name,) in tables:
        if "thread_id" not in {col[1] for col in c.execute(f"PRAGMA table_info({table_name})").fetchall()}: continue
        c.execute(f"DELETE FROM {table_name} WHERE thread_id IN ({placeholders})", tuple(ids))
        deleted += c.rowcount
    conn.commit()
    conn.close()
    return deleted
