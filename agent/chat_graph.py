from __future__ import annotations

import atexit
import os
import sqlite3
from typing import Annotated, Any, Dict, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, trim_messages, HumanMessage
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
from utils.logger import setup_logger

load_dotenv()
logger = setup_logger("ChatGraph")

CHAT_CHECKPOINT_DB = os.getenv("CHAT_CHECKPOINT_DB", "chat_checkpoints.sqlite")
CHAT_MAX_HISTORY_MESSAGES = int(os.getenv("CHAT_MAX_HISTORY_MESSAGES", "15"))


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
        model_kwargs=kwargs,
    ).bind_tools(_get_chat_tools(mode))

    history = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
    llm_messages = [SystemMessage(content=state.get("system_prompt", ""))] + history
    trimmed = trim_messages(
        llm_messages,
        max_tokens=CHAT_MAX_HISTORY_MESSAGES,
        token_counter=_message_counter,
        strategy="last",
        include_system=True,
        allow_partial=False,
    )

    response = llm.invoke(trimmed)
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


def stream_chat(session_id: str, payload: Dict[str, Any]):
    """
    Streams the chat response from the LLM, bypassing the main graph but using its components.
    This is a generator that yields chunks of the response.
    """
    config = {"configurable": {"thread_id": session_id}}
    
    # 1. Get current state
    snapshot = chat_app.get_state(config)
    
    # 2. Prepare for LLM call (similar to start_node and model_node)
    human_message = payload["messages"][0]
    cfg = payload.get("agent_config")
    if not cfg:
        raise ValueError("Config not found in payload")

    # Get system prompt
    scheduler_state = AgentState(
        config_id=payload["config_id"],
        symbol=payload["symbol"],
        messages=[],
        agent_config=cfg,
        market_context={},
        account_context={},
        history_context=[],
        full_analysis="",
        human_message=None,
    )
    started = scheduler_start_node(scheduler_state)
    system_prompt_content = started.messages[0].content if started.messages else ""

    # Prepare messages
    history = [m for m in snapshot.values.get("messages", []) if not isinstance(m, SystemMessage)]
    llm_messages = [SystemMessage(content=system_prompt_content)] + history + [human_message]
    
    trimmed = trim_messages(
        llm_messages,
        max_tokens=CHAT_MAX_HISTORY_MESSAGES,
        strategy="last",
        include_system=True,
    )

    # 3. Instantiate LLM and stream
    mode = cfg.get("mode", "STRATEGY").upper()
    llm = ChatOpenAI(
        model=cfg.get("model"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("api_base"),
        temperature=cfg.get("temperature", 0.5),
    ).bind_tools(_get_chat_tools(mode))

    full_response = None
    for chunk in llm.stream(trimmed):
        yield chunk
        if full_response is None:
            full_response = chunk
        else:
            full_response += chunk
    
    # 4. Update state with the new messages
    if full_response:
        # Manually add the human message that initiated the stream
        final_messages = [human_message, full_response]
        chat_app.update_state(config, {"messages": final_messages})


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
