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
    analyze_event_contract,
    format_event_contract_order
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


def _get_chat_tools(cfg: Dict[str, Any]):
    trade_mode = cfg.get("mode", "STRATEGY").upper()
    
    if trade_mode == "REAL":
        return [open_position_real, close_position_real, cancel_orders_real, analyze_event_contract, format_event_contract_order]
    return [open_position_strategy, cancel_orders_strategy, analyze_event_contract, format_event_contract_order]


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
    q = state.get("q")
    
    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        raise ValueError(f"Config not found for config_id={config_id}")

    symbol = cfg.get("symbol", "Unknown")
    
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
    history = list(state.get("messages", []))
    system_prompt = state.get("system_prompt", "")
    trimmed = _trim_chat_messages(system_prompt, history)
    
    has_human = any(isinstance(m, HumanMessage) for m in trimmed)
    logger.info(f"LLM Input: {len(trimmed)} msgs (Has Human: {has_human}), model: {cfg.get('model')}")

    kwargs = {}
    if cfg.get("extra_body"):
        kwargs["extra_body"] = cfg.get("extra_body")

    # IMPORTANT: keep non-streaming here. streaming=True can make invoke()
    # return a generator in some LangChain versions, which breaks add_messages.
    llm = ChatOpenAI(
        model=cfg.get("model"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("api_base"),
        temperature=0.5,
        streaming=False,
        model_kwargs=kwargs,
    ).bind_tools(_get_chat_tools(cfg))

    # 使用 invoke 而不是 stream，避免将生成器返回给 Annotated[list, add_messages] 导致报错
    # LangGraph 的 add_messages reducer 无法处理生成器类型
    response = llm.invoke(trimmed)
    
    # 统一处理 DeepSeek 等模型的思维链 (reasoning_content)
    reasoning = response.additional_kwargs.get("reasoning_content") or response.response_metadata.get("reasoning_content")
    if reasoning and not response.tool_calls and "<thinking>" not in response.content:
        response.content = f"<thinking>\n{reasoning}\n</thinking>\n\n{response.content}"

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
        "analyze_event_contract": analyze_event_contract,
        "format_event_contract_order": format_event_contract_order,
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

        # 事件合约分析工具免审批
        if tool_name in ["analyze_event_contract", "format_event_contract_order"]:
            approved = True
        else:
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
    add_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
    resp_meta = getattr(chunk, "response_metadata", {}) or {}
    
    if "reasoning_content" in add_kwargs:
        return _coerce_text(add_kwargs["reasoning_content"])
    
    if "reasoning_content" in resp_meta:
        return _coerce_text(resp_meta["reasoning_content"])

    delta = add_kwargs.get("delta") or resp_meta.get("delta") or {}
    if isinstance(delta, dict):
        res = delta.get("reasoning_content") or delta.get("reasoning")
        if res:
            return _coerce_text(res)
            
    return ""


def _extract_tool_calls(chunk: BaseMessageChunk | Any) -> list:
    """提取增量的工具调用块"""
    tcc = getattr(chunk, "tool_call_chunks", [])
    if not tcc: return []
    
    res = []
    for c in tcc:
        res.append({
            "index": c.get("index"),
            "id": c.get("id"),
            "name": c.get("name"),
            "args": c.get("args"),
        })
    return res


def stream_chat(session_id: str, payload: Dict[str, Any]):
    config = {"configurable": {"thread_id": session_id}}
    for item in chat_app.stream(payload, config=config, stream_mode="messages"):
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        chunk, metadata = item
        if not metadata or metadata.get("langgraph_node") != "model":
            continue
            
        reasoning_token = _chunk_reasoning_text(chunk)
        if reasoning_token:
            yield {"type": "reasoning_token", "token": reasoning_token}
            
        token = _chunk_to_text(chunk)
        if token:
            yield {"type": "token", "token": token}
            
        tool_calls = _extract_tool_calls(chunk)
        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls}


def stream_resume_chat(session_id: str, approved: bool):
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

        tool_calls = _extract_tool_calls(chunk)
        if tool_calls:
            yield {"type": "tool_calls", "tool_calls": tool_calls}


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
