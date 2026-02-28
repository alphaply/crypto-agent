from __future__ import annotations

import atexit
import os
import sqlite3
from typing import Annotated, Any, Dict, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import (
    BaseMessageChunk,
    HumanMessage,
    ToolMessage,
    trim_messages,
)
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from agent.agent_graph import start_node as scheduler_start_node
from agent.agent_models import AgentState
from agent.agent_tools import (
    cancel_orders_real,
    cancel_orders_strategy,
    close_position_real,
    open_position_real,
    open_position_strategy,
)
from config import config as global_config
import database
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger("ChatGraph")

CHAT_CHECKPOINT_DB = os.getenv("CHAT_CHECKPOINT_DB", "chat_checkpoints.sqlite")
CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CHAT_MAX_HISTORY_MESSAGES", "8"))
CHAT_TRIM_MAX_TOKENS = int(os.getenv("CHAT_TRIM_MAX_TOKENS", "6000"))


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    config_id: str
    symbol: str
    agent_config: Dict[str, Any]
    system_prompt: str


def _get_chat_tools(trade_mode: str):
    if trade_mode == "REAL":
        return [open_position_real, close_position_real, cancel_orders_real]
    return [open_position_strategy, cancel_orders_strategy]


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
    """
    Keep only tool-call spans that are protocol-complete:
    assistant(tool_calls) + contiguous matching tool messages.
    Drop orphan ToolMessage or incomplete tool-call spans (usually caused by trim boundary).
    """
    sanitized = []
    i = 0
    while i < len(messages):
        msg = messages[i]

        if isinstance(msg, ToolMessage):
            # Orphan tool message.
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
        # else: incomplete span, drop it.
        i = j

    return sanitized


def _trim_chat_messages(system_prompt: str, history: list):
    # Force the market-context prompt to be a HumanMessage and never trim it.
    pinned_prompt = HumanMessage(content=system_prompt)

    # Stage 1: token trim on history only.
    if history:
        token_trimmed_history = trim_messages(
            history,
            max_tokens=CHAT_TRIM_MAX_TOKENS,
            token_counter="approximate",
            strategy="last",
            include_system=False,
            allow_partial=False,
        )
    else:
        token_trimmed_history = []
    token_trimmed_history = _sanitize_tool_sequences(token_trimmed_history)

    # Stage 2: strict message-count trim on history only.
    tail_limit = max(0, CHAT_MAX_HISTORY_MESSAGES - 1)  # reserve 1 for pinned_prompt
    if tail_limit == 0 or not token_trimmed_history:
        count_trimmed_history = []
    else:
        count_trimmed_history = trim_messages(
            token_trimmed_history,
            max_tokens=tail_limit,
            token_counter=_message_counter,
            strategy="last",
            include_system=False,
            allow_partial=False,
        )
    count_trimmed_history = _sanitize_tool_sequences(count_trimmed_history)

    final_messages = [pinned_prompt] + count_trimmed_history
    return final_messages


def start_node(state: ChatState):
    cfg = state.get("agent_config") or global_config.get_config_by_id(state["config_id"])
    if not cfg:
        raise ValueError(f"Config not found for config_id={state['config_id']}")

    # Reuse the exact scheduler prompt/data flow to keep chat/system prompt consistent.
    scheduler_state = AgentState(
        config_id=state["config_id"],
        symbol=state["symbol"],
        messages=[],
        agent_config=cfg,
        market_context={},
        account_context={},
        history_context=[],
        full_analysis="",
        human_message=None,
    )
    started = scheduler_start_node(scheduler_state)
    system_prompt = started.messages[0].content if started.messages else ""
    return {"agent_config": cfg, "system_prompt": system_prompt}


def model_node(state: ChatState):
    cfg = state.get("agent_config") or global_config.get_config_by_id(state["config_id"])
    if not cfg:
        raise ValueError(f"Config not found for config_id={state['config_id']}")

    mode = cfg.get("mode", "STRATEGY").upper()
    kwargs = {}
    if cfg.get("extra_body"):
        kwargs["extra_body"] = cfg.get("extra_body")

    llm = ChatOpenAI(
        model=cfg.get("model"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("api_base"),
        temperature=cfg.get("temperature", 0.5),
        streaming=True,
        model_kwargs={
            **kwargs,
            "stream_options": {"include_usage": True}
        },
    ).bind_tools(_get_chat_tools(mode))

    history = state["messages"]
    # --- 修复 DeepSeek 格式问题 ---
    sanitized_history = []
    for msg in history:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            if "reasoning_content" not in msg.additional_kwargs:
                msg.additional_kwargs["reasoning_content"] = msg.response_metadata.get("reasoning_content", "")
        sanitized_history.append(msg)

    trimmed = _trim_chat_messages(state.get("system_prompt", ""), sanitized_history)

    # 在 LangGraph 中，invoke 依然会等待流结束并聚合结果
    response = llm.invoke(trimmed)
    
    # --- DeepSeek 思维链展示优化 ---
    # 如果模型返回了 reasoning_content (DeepSeek 特有)，将其包装进 content
    reasoning = response.additional_kwargs.get("reasoning_content") or response.response_metadata.get("reasoning_content")
    if reasoning and "<thinking>" not in response.content:
        response.content = f"<thinking>\n{reasoning}\n</thinking>\n\n{response.content}"

    # 记录 Token 使用情况
    try:
        # 注意：某些模型在流聚合后的 usage 字段路径可能略有不同，这里做兼容处理
        usage = response.response_metadata.get("token_usage") or getattr(response, "usage_metadata", None)
        if usage:
            database.save_token_usage(
                symbol=state.get("symbol", "Chat"),
                config_id=state.get("config_id", "chat-user"),
                model=cfg.get("model"),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0)
            )
            logger.info(f"📊 [Chat Stream] Tokens saved: {usage.get('total_tokens', 0)}")
        else:
            logger.warning("⚠️ [Chat Stream] No token_usage found in stream_metadata")
    except Exception as usage_e:
        logger.warning(f"⚠️ [Chat] Failed to save token usage: {usage_e}")

    return {"messages": [response], "agent_config": cfg}


def _run_tool(tool_name: str, args: Dict[str, Any], config_id: str, symbol: str) -> str:
    tool_map = {
        "open_position_real": open_position_real,
        "close_position_real": close_position_real,
        "cancel_orders_real": cancel_orders_real,
        "open_position_strategy": open_position_strategy,
        "cancel_orders_strategy": cancel_orders_strategy,
    }
    tool_obj = tool_map.get(tool_name)
    if not tool_obj:
        return f"Error: Tool '{tool_name}' not found."

    call_args = dict(args)
    call_args["config_id"] = config_id
    call_args["symbol"] = symbol
    return str(tool_obj.func(**call_args))


def tools_node(state: ChatState):
    last_message = state["messages"][-1]
    tool_calls = getattr(last_message, "tool_calls", []) or []
    outputs = []

    for call in tool_calls:
        tool_name = call["name"]
        tool_args = call.get("args", {})

        approval = interrupt(
            {
                "type": "tool_approval",
                "tool_call_id": call["id"],
                "tool_name": tool_name,
                "tool_args": tool_args,
                "config_id": state["config_id"],
                "symbol": state["symbol"],
            }
        )

        approved = False
        if isinstance(approval, dict):
            approved = bool(approval.get("approved"))
        elif isinstance(approval, bool):
            approved = approval

        if not approved:
            outputs.append(
                ToolMessage(
                    tool_call_id=call["id"],
                    content="Tool execution rejected by user.",
                )
            )
            continue

        try:
            result = _run_tool(tool_name, tool_args, state["config_id"], state["symbol"])
            outputs.append(ToolMessage(tool_call_id=call["id"], content=result))
        except Exception as exc:
            logger.error(f"Tool execution failed ({tool_name}): {exc}")
            outputs.append(ToolMessage(tool_call_id=call["id"], content=f"Error: {exc}"))

    return {"messages": outputs}


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
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def stream_chat(session_id: str, payload: Dict[str, Any]):
    """Token-level stream from the compiled LangGraph execution."""
    config = {"configurable": {"thread_id": session_id}}
    for item in chat_app.stream(payload, config=config, stream_mode="messages"):
        # LangGraph returns (message_chunk, metadata) in messages mode.
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        chunk, metadata = item
        if not metadata or metadata.get("langgraph_node") != "model":
            continue
        token = _chunk_to_text(chunk)
        if token:
            yield token


def stream_resume_chat(session_id: str, approved: bool):
    """Token-level stream for resume after human approval."""
    config = {"configurable": {"thread_id": session_id}}
    command = Command(resume={"approved": approved})
    for item in chat_app.stream(command, config=config, stream_mode="messages"):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        chunk, metadata = item
        if not metadata or metadata.get("langgraph_node") != "model":
            continue
        token = _chunk_to_text(chunk)
        if token:
            yield token


def invoke_chat(session_id: str, payload: Dict[str, Any]):
    config = {"configurable": {"thread_id": session_id}}
    return chat_app.invoke(payload, config=config)


def resume_chat(session_id: str, approved: bool):
    config = {"configurable": {"thread_id": session_id}}
    return chat_app.invoke(Command(resume={"approved": approved}), config=config)


def get_chat_state(session_id: str):
    config = {"configurable": {"thread_id": session_id}}
    snapshot = chat_app.get_state(config)
    return snapshot.values if snapshot else {}


def get_chat_interrupt(session_id: str):
    config = {"configurable": {"thread_id": session_id}}
    snapshot = chat_app.get_state(config)
    if not snapshot or not getattr(snapshot, "interrupts", None):
        return None
    intr = snapshot.interrupts[0]
    return {
        "id": getattr(intr, "id", ""),
        "value": getattr(intr, "value", {}) or {},
    }


def delete_chat_threads(session_ids):
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return 0

    # Ensure pending writes are flushed before direct sqlite cleanup.
    checkpointer.conn.commit()

    conn = sqlite3.connect(CHAT_CHECKPOINT_DB)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    deleted = 0
    placeholders = ",".join(["?"] * len(ids))
    for (table_name,) in tables:
        cols = c.execute(f"PRAGMA table_info({table_name})").fetchall()
        col_names = {col[1] for col in cols}
        if "thread_id" not in col_names:
            continue
        c.execute(
            f"DELETE FROM {table_name} WHERE thread_id IN ({placeholders})",
            tuple(ids),
        )
        deleted += c.rowcount

    conn.commit()
    conn.close()
    return deleted
