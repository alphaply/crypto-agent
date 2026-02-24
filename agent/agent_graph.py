import uuid
import json
import re
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from agent.agent_models import AgentState
from agent.agent_tools import open_position, close_position, cancel_orders
from utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, \
    format_market_data_to_text
from utils.logger import setup_logger
from utils.prompt_utils import resolve_prompt_template, render_prompt

import database
from utils.market_data import MarketTool
from config import config as global_config

TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("AgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent

# ==========================================
# 1. Summarizer Pipeline
# ==========================================

def summarize_content(content: str, agent_config: dict) -> str:
    """使用独立的 LLM 配置对分析内容进行压缩。"""
    summarizer_cfg = agent_config.get("summarizer", {})
    
    # 获取配置，优先级：1. agent专属summarizer -> 2. 全局环境变量 -> 3. agent自身配置
    model = (summarizer_cfg.get("model") or 
             os.getenv("GLOBAL_SUMMARIZER_MODEL") or 
             agent_config.get("model"))
    api_key = (summarizer_cfg.get("api_key") or 
               os.getenv("GLOBAL_SUMMARIZER_API_KEY") or 
               agent_config.get("api_key"))
    api_base = (summarizer_cfg.get("api_base") or 
                os.getenv("GLOBAL_SUMMARIZER_API_BASE") or 
                agent_config.get("api_base"))
    temperature = summarizer_cfg.get("temperature", 0.3)
    
    logger.info(f"--- [Pipeline] Summarizing content for history using {model} ---")
    
    try:
        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature
        )
        prompt = f"""请将以下交易分析内容压缩为一段简短的“策略逻辑”（150字以内），保留核心观点、关键点位和操作意图。
直接输出压缩后的文字，不要有任何前缀。

内容：
{content}
"""
        response = llm.invoke([SystemMessage(content=prompt)])
        return response.content.strip()
    except Exception as e:
        logger.error(f"❌ [Summarizer Error]: {e}")
        return content[:200] + "..."

# ==========================================
# 2. Nodes
# ==========================================

def start_node(state: AgentState) -> AgentState:
    config_id = state.config_id
    symbol = state.symbol
    config = state.agent_config
    now = datetime.now(TZ_CN)
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now.weekday()]})"

    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    agent_name = config_id

    market_tool = MarketTool(config_id=config_id)
    logger.info(f"--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec, agent_name=agent_name)
        recent_summaries = database.get_recent_summaries(symbol, agent_name=agent_name, limit=4)
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        recent_summaries = []

    if is_real_exec:
        try:
            balance = account_data.get('balance', 0)
            positions = account_data.get('real_positions', [])
            total_unrealized_pnl = sum([float(p.get('unrealized_pnl', 0)) for p in positions])
            database.save_balance_snapshot(symbol, balance, total_unrealized_pnl)
            recent_trades = market_tool.fetch_recent_trades(symbol, limit=10)
            if recent_trades:
                database.save_trade_history(recent_trades)
        except Exception as e:
            logger.error(f"❌ Failed to save real-time stats: {e}")

    balance = account_data.get('balance', 0)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0

    indicators_summary = {}
    timeframes = ['1h', '4h', '1d', '1w'] if trade_mode == 'STRATEGY' else ['15m', '1h', '4h', '1d']
    raw_analysis = market_full.get("analysis", {})

    for tf in timeframes:
        if tf not in raw_analysis: continue
        tf_data = raw_analysis[tf]
        indicators_summary[tf] = {
            "price": tf_data.get("price"),
            "trend_status": tf_data.get("trend_status", "N/A"),
            "recent_closes": tf_data.get("recent_closes", []),
            "ema": tf_data.get("ema"),
            "rsi": tf_data.get("rsi"),
            "atr": tf_data.get("atr"),
            "macd": tf_data.get("macd"),
            "bollinger": tf_data.get("bollinger"),
            "volume_status": tf_data.get("volume_analysis", {}).get("status"),
        }

    market_context_llm = {
        "current_price": current_price,
        "atr_15m": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary
    }

    formatted_market_data = format_market_data_to_text(market_context_llm)
    history_entries = []
    if recent_summaries:
        for s in recent_summaries:
            logic = s.get('strategy_logic') or s.get('content', '')
            if "LLM Failed" in logic: continue
            history_entries.append(f" [{s.get('timestamp', 'Unknown')}] Logic: {logic}")
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    prompt_template = resolve_prompt_template(config, trade_mode, PROJECT_ROOT, logger)
    leverage = global_config.get_leverage(config_id)

    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [{"id": o.get('order_id'), "side": o.get('side'), "pos_side": o.get('pos_side'), "price": o.get('price'), "amount": o.get('amount')} for o in raw_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_orders)
    else:
        raw_mock_orders = account_data.get('mock_open_orders', [])
        display_mock_orders = [{"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'), "tp": o.get('take_profit'), "sl": o.get('stop_loss')} for o in raw_mock_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_mock_orders)

    system_prompt = render_prompt(
        prompt_template,
        model=config.get('model'),
        symbol=symbol,
        leverage=leverage,
        current_time=current_time_str,
        current_price=current_price,
        atr_15m=atr_15m,
        balance=balance,
        positions_text=positions_text,
        orders_text=orders_friendly_text,
        formatted_market_data=formatted_market_data,
        history_text=formatted_history_text
    )

    return state.model_copy(update={
        "market_context": market_full,
        "account_context": account_data,
        "history_context": recent_summaries,
        "messages": [SystemMessage(content=system_prompt)]
    })

def agent_node(state: AgentState) -> AgentState:
    config = state.agent_config
    symbol = state.symbol
    logger.info(f"--- [Node] Agent: {config.get('model')} ---")

    try:
        kwargs = {}
        if config.get('extra_body'):
            kwargs["extra_body"] = config.get('extra_body')

        llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5),
            model_kwargs=kwargs
        ).bind_tools([open_position, close_position, cancel_orders])

        response = llm.invoke(state.messages)
        return state.model_copy(update={"messages": state.messages + [response]})

    except Exception as e:
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")]})

def tools_node(state: AgentState) -> AgentState:
    """通用的工具执行节点。"""
    last_message = state.messages[-1]
    tool_calls = getattr(last_message, 'tool_calls', [])
    
    config_id = state.config_id
    symbol = state.symbol
    trade_mode = state.agent_config.get('mode', 'STRATEGY').upper()
    
    tool_outputs = []
    available_tools = {
        "open_position": open_position,
        "close_position": close_position,
        "cancel_orders": cancel_orders
    }
    
    for tool_call in tool_calls:
        tool_name = tool_call['name']
        args = tool_call['args']
        logger.info(f"🛠️ ToolNode Dispatching: {tool_name}")
        
        if tool_name in available_tools:
            # 注入上下文
            args['config_id'] = config_id
            args['symbol'] = symbol
            args['trade_mode'] = trade_mode
            result = available_tools[tool_name].invoke(args)
            tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=str(result)))
        else:
            tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=f"Error: Tool '{tool_name}' not found."))
            
    return state.model_copy(update={"messages": state.messages + tool_outputs})

def finalize_node(state: AgentState) -> AgentState:
    """处理最终文本输出并保存。"""
    config_id = state.config_id
    symbol = state.symbol
    agent_name = config_id
    
    analysis_msg = None
    for msg in reversed(state.messages):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            analysis_msg = msg
            break
            
    if analysis_msg:
        content = analysis_msg.content
        strategy_logic = summarize_content(content, state.agent_config)
        try:
            database.save_summary(symbol, agent_name, content, strategy_logic)
        except Exception as e:
            logger.warning(f"⚠️ Save summary failed: {e}")
    return state

def should_continue(state: AgentState):
    last_message = state.messages[-1]
    if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "tools"
    return "finalize"

# ==========================================
# 4. Graph Construction
# ==========================================

workflow = StateGraph(AgentState)
workflow.add_node("start", start_node)
workflow.add_node("agent", agent_node)
workflow.add_node("tools", tools_node)
workflow.add_node("finalize", finalize_node)

workflow.set_entry_point("start")
workflow.add_edge("start", "agent")

workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
workflow.add_edge("tools", "agent")
workflow.add_edge("finalize", END)

app = workflow.compile(name='Crypto Agent')

def run_agent_for_config(config: dict):
    config_id = config.get('config_id', 'unknown')
    symbol = config['symbol']
    initial_state = AgentState(
        config_id=config_id,
        symbol=symbol,
        messages=[],
        agent_config=config,
        market_context={},
        account_context={},
        history_context=[],
        full_analysis=""
    )
    try:
        app.invoke(initial_state)
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
