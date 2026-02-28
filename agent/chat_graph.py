from __future__ import annotations

import atexit
import os
import sqlite3
from typing import Annotated, Any, Dict, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import (
    BaseMessageChunk,
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
    trim_messages,
    BaseMessage
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
CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CHAT_MAX_HISTORY_MESSAGES", "12"))
CHAT_TRIM_MAX_TOKENS = int(os.getenv("CHAT_TRIM_MAX_TOKENS", "6000"))


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    config_id: str
    symbol: str
    agent_config: Dict[str, Any]
    system_prompt: str
    q: str


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


def _trim_chat_messages(system_prompt: str, history: list):
    pinned_prompt = HumanMessage(content=system_prompt)
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
    # 确保最近的几条消息始终存在，避免 trim 过度导致用户输入丢失
    count_trimmed_history = trim_messages(
        token_trimmed_history,
        max_tokens=CHAT_MAX_HISTORY_MESSAGES,
        token_counter=_message_counter,
        strategy="last",
        include_system=False,
        allow_partial=False,
    )
    count_trimmed_history = _sanitize_tool_sequences(count_trimmed_history)
    final_messages = [SystemMessage(content=system_prompt)] + count_trimmed_history
    return final_messages


def start_node(state: ChatState):
    config_id = state.get("config_id")
    # 不要清空 q，让 model_node 也能看到它作为兜底
    q = state.get("q")
    
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError(f"Config not found for config_id={config_id}")

    symbol = cfg.get("symbol", "Unknown")
    
    # 模拟调度器的启动逻辑来获取系统提示词
    scheduler_state = AgentState(
        config_id=config_id,
        symbol=symbol,
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
    
    # 构造返回给 LangGraph 的 updates
    # 注意：这里我们只提供基础信息，messages 的具体构造我们在 model_node 里精细化处理
    updates = {
        "agent_config": cfg, 
        "system_prompt": system_prompt, 
        "symbol": symbol,
        "q": q
    }
    if q:
        updates["messages"] = [HumanMessage(content=q)]
    return updates


def model_node(state: ChatState):
    config_id = state.get("config_id")
    cfg = state.get("agent_config") or global_config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError(f"Config context lost for config_id={config_id}")

    symbol = cfg.get("symbol", "Chat")
    mode = cfg.get("mode", "STRATEGY").upper()
    
    # --- 核心修复：构造消息队列 ---
    history = list(state.get("messages", []))
    # 这里的 history 应该已经通过 start_node 包含了当前用户输入的 HumanMessage

    sanitized_history = []
    for msg in history:
        # DeepSeek 修复
        if isinstance(msg, AIMessage) and msg.tool_calls:
            if "reasoning_content" not in msg.additional_kwargs:
                reasoning = msg.response_metadata.get("reasoning_content") or ""
                msg.additional_kwargs["reasoning_content"] = reasoning
        sanitized_history.append(msg)

    # 构造最终列表
    system_prompt = state.get("system_prompt", "")
    trimmed = _trim_chat_messages(system_prompt, sanitized_history)
    
    # 日志监控：确保 HumanMessage 在列表里
    has_human = any(isinstance(m, HumanMessage) for m in trimmed)
    logger.info(f"LLM Input: {len(trimmed)} msgs (Has Human: {has_human}), model: {cfg.get('model')}")
    if not has_human and len(trimmed) > 0:
        logger.warning("!!! WARNING: No HumanMessage found in LLM prompt!")

    # --- 调用 LLM ---
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

    response = llm.invoke(trimmed)
    
    # 推理内容处理
    reasoning = response.additional_kwargs.get("reasoning_content") or \
                response.response_metadata.get("reasoning_content") or ""
    
    if reasoning and "<thinking>" not in response.content:
        response.additional_kwargs["reasoning_content"] = reasoning
        response.content = f"<thinking>\n{reasoning}\n</thinking>\n\n{response.content}"

    # 保存 Token 使用情况
    try:
        usage = response.response_metadata.get("token_usage") or getattr(response, "usage_metadata", None)
        if usage:
            database.save_token_usage(
                symbol=symbol,
                config_id=config_id or "chat-user",
                model=cfg.get("model"),
                prompt_tokens=getattr(usage, 'prompt_tokens', usage.get("prompt_tokens", 0)) if hasattr(usage, 'prompt_tokens') else usage.get("prompt_tokens", 0),
                completion_tokens=getattr(usage, 'completion_tokens', usage.get("completion_tokens", 0)) if hasattr(usage, 'completion_tokens') else usage.get("completion_tokens", 0)
            )
    except Exception as usage_e:
        logger.warning(f"⚠️ [Chat] Token save failed: {usage_e}")

    # 这里的返回将自动更新 LangGraph 状态中的 messages (由于 Annotated[list, add_messages])
    # 同时我们要清空 q，并带上 agent_config 以防状态丢失
    return {
        "messages": [response], 
        "symbol": symbol, 
        "q": None, 
        "agent_config": cfg
    }


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
    config_id = state["config_id"]
    cfg = state.get("agent_config") or global_config.get_config_by_id(config_id)
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
        return "".join([i.get("text", "") if isinstance(i, dict) else str(i) for i in content])
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
        for key in ("text", "content", "reasoning_content", "delta"):
            if key in value:
                return _coerce_text(value.get(key))
        return ""
    return str(value)


def _chunk_reasoning_text(chunk: BaseMessageChunk | Any) -> str:
    # 检查所有可能的推理字段位置，确保 DeepSeek 的响应不丢失
    add_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
    resp_meta = getattr(chunk, "response_metadata", {}) or {}
    
    # 路径 1: 直接在 additional_kwargs
    if "reasoning_content" in add_kwargs:
        return _coerce_text(add_kwargs["reasoning_content"])
    
    # 路径 2: 在 response_metadata
    if "reasoning_content" in resp_meta:
        return _coerce_text(resp_meta["reasoning_content"])

    # 路径 3: 在 delta 字典内 (DeepSeek 原生格式)
    delta = add_kwargs.get("delta") or resp_meta.get("delta") or {}
    if isinstance(delta, dict):
        # 同时检查 reasoning_content 和 reasoning 字段
        res = delta.get("reasoning_content") or delta.get("reasoning")
        if res:
            return _coerce_text(res)
            
    # 路径 4: 部分兼容层会放在 content 字段里但带有特殊前缀，
    # 这种情况通过 stream_chat 里的 chunk.content 正常处理。
    
    return ""


def stream_chat(session_id: str, payload: Dict[str, Any]):
    config = {"configurable": {"thread_id": session_id}}
    
    # 显式构造输入，确保用户消息 q 被 LangGraph 接收到
    for item in chat_app.stream(payload, config=config, stream_mode="messages"):
        # LangGraph 2.0+ stream_mode="messages" 返回的是 (chunk, metadata)
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        
        chunk, metadata = item
        # 只处理 model 节点的输出
        if not metadata or metadata.get("langgraph_node") != "model":
            continue

        # 处理思考过程 (Reasoning)
        reasoning_token = _chunk_reasoning_text(chunk)
        if reasoning_token:
            yield {"type": "reasoning_token", "token": reasoning_token}

        # 处理正文 (Content)
        token = _chunk_to_text(chunk)
        if token:
            yield {"type": "token", "token": token}


def stream_resume_chat(session_id: str, approved: bool):
    """Token-level stream for resume after human approval."""
    config = {"configurable": {"thread_id": session_id}}
    command = Command(resume={"approved": approved})

    for item in chat_app.stream(command, config=config, stream_mode="messages"):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        chunk, metadata = item
        node = metadata.get("langgraph_node", "")
        if node not in ["model", "tools"]:
            continue

        reasoning_token = _chunk_reasoning_text(chunk)
        if reasoning_token:
            yield {"type": "reasoning_token", "token": reasoning_token}

        token = _chunk_to_text(chunk)
        if token:
            yield {"type": "token", "token": token}


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
