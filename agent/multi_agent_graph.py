import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from agent.agent_models import AgentState, ScreenerResult
from agent.agent_tools import (
    open_position_real, close_position_real, cancel_orders_real
)
from agent.agent_graph import summarize_content, finalize_node, tools_node
from utils.formatters import (
    format_positions_to_agent_friendly, format_orders_to_agent_friendly,
    format_market_data_to_text, escape_markdown_special_chars
)
from utils.logger import setup_logger
from utils.prompt_utils import render_prompt
from utils.market_data import MarketTool
from config import config as global_config
import database

TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))
logger = setup_logger("MultiAgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ==========================================
# Nodes for Multi Agent
# ==========================================

def _get_screener_prompt(agent_config: dict, symbol: str, current_price: float, formatted_market_data: str) -> str:
    prompt_file = agent_config.get("prompt_file", "multi_agent_screener.txt")
    candidate = PROJECT_ROOT / "agent" / "prompts" / prompt_file
    if candidate.exists():
        template = candidate.read_text(encoding="utf-8").strip()
    else:
        template = "你是一个快速筛选市场行情的机器人，现在价格 {current_price}，分析以下数据决断是否升级给主模型：\n{formatted_market_data}\n"
        logger.warning(f"Screener prompt file not found: {candidate}")

    now_cn = datetime.now(TZ_CN)
    current_time_str = now_cn.strftime('%Y-%m-%d %H:%M:%S')

    return render_prompt(
        template,
        current_time=current_time_str,
        symbol=symbol,
        current_price=current_price,
        formatted_market_data=formatted_market_data
    )

def _get_analyst_prompt(agent_config: dict, state: AgentState) -> str:
    from agent.agent_graph import calculate_next_run_time
    now_cn = datetime.now(TZ_CN)
    now_us = datetime.now(pytz.timezone('America/New_York'))
    week_map_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    current_time_str = (
        f"北京: {now_cn.strftime('%Y-%m-%d %H:%M:%S')} (周{['一', '二', '三', '四', '五', '六', '日'][now_cn.weekday()]}) | "
        f"美东: {now_us.strftime('%Y-%m-%d %H:%M:%S')} ({week_map_en[now_us.weekday()]})"
    )

    market_full = state.market_context
    account_data = state.account_context
    daily_history = state.history_context
    symbol = state.symbol
    config_id = agent_config.get('config_id')
    trade_mode = agent_config.get("mode", "MULTI_AGENT")

    balance = account_data.get('balance', 0)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0

    indicators_summary = {}
    timeframes = ['15m', '1h', '4h', '1d']
    raw_analysis = market_full.get("analysis", {})
    for tf in timeframes:
        if tf in raw_analysis:
            tf_data = raw_analysis[tf]
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "trend": tf_data.get("trend", {}),
                "ema": tf_data.get("ema"),
                "rsi_analysis": tf_data.get("rsi_analysis", {}),
                "atr": tf_data.get("atr"),
                "macd": tf_data.get("macd"),
                "bollinger": tf_data.get("bollinger"),
                "vp": tf_data.get("vp", {}),
                "volume_analysis": tf_data.get("volume_analysis", {}),
            }
            if tf_data.get("vwap") is not None:
                 indicators_summary[tf]["vwap"] = tf_data["vwap"]

    market_context_llm = {
        "current_price": current_price,
        "atr_base": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary
    }

    formatted_market_data = format_market_data_to_text(market_context_llm)
    history_entries = []
    if daily_history:
        for ds in daily_history:
            history_entries.append(f"  [{ds.get('date', '')}] ({ds.get('source_count', 0)}轮) {ds.get('summary', '')}")
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    # 这里需要确保 MULTI_AGENT 实际上使用了大模型的 prompt, 比如 real.txt
    analyst_prompt_file = agent_config.get("analyst_prompt_file", "real.txt")
    candidate = PROJECT_ROOT / "agent" / "prompts" / analyst_prompt_file
    if candidate.exists():
        template = candidate.read_text(encoding="utf-8").strip()
    else:
        template = "分析市场。\n{formatted_market_data}"

    next_run_time = calculate_next_run_time(agent_config, now_cn)
    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    leverage = global_config.get_leverage(config_id)

    raw_orders = account_data.get('real_open_orders', [])
    display_orders = [{"id": o.get('order_id'), "side": o.get('side'), "pos_side": o.get('pos_side'), "price": o.get('price'), "amount": o.get('amount')} for o in raw_orders]
    orders_friendly_text = format_orders_to_agent_friendly(display_orders)

    return render_prompt(
        template,
        model=agent_config.get('analyst_model'),
        symbol=symbol,
        leverage=leverage,
        current_time=current_time_str,
        next_run_time=next_run_time,
        current_price=current_price,
        atr_15m=atr_15m,
        balance=balance,
        positions_text=positions_text,
        orders_text=orders_friendly_text,
        formatted_market_data=formatted_market_data,
        history_text=formatted_history_text,
    )


def screener_start_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    symbol = state.symbol
    logger.info(f"--- [Node] Screener Start: Fetching basic data for {symbol} ---")

    market_tool = MarketTool(config_id=config_id)
    try:
        # 只获取必要几个轻量周期，省 token
        timeframes_to_fetch = ['15m', '1h', '4h']
        market_full = market_tool.get_market_analysis(symbol, mode='MULTI_AGENT', timeframes=timeframes_to_fetch)
    except Exception as e:
        logger.error(f"❌ [Screener Data Fetch Error]: {e}")
        market_full = {}

    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)

    # 简单格式化提供给 screener
    indicators_summary = {}
    for tf in timeframes_to_fetch:
        if tf in market_full.get("analysis", {}):
            d = market_full["analysis"][tf]
            indicators_summary[tf] = {
                "price": d.get("price"),
                "trend": d.get("trend"),
                "rsi_analysis": d.get("rsi_analysis"),
                "macd": d.get("macd"),
                "bollinger": d.get("bollinger"),
            }
    
    market_context_llm = {
        "current_price": current_price,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary
    }

    formatted_market_data = format_market_data_to_text(market_context_llm)
    prompt = _get_screener_prompt(agent_config, symbol, current_price, formatted_market_data)

    return state.model_copy(update={
        "market_context": market_full,
        "messages": [HumanMessage(content=prompt)]
    })

from langchain_core.tools import tool

@tool
def submit_screener_decision(confidence: int, should_escalate: bool, market_status: str, reason: str):
    """
    提交筛选决策。
    confidence: 0-100的整数，表示机会概率。
    should_escalate: 布尔值，是否建议升级到大模型。
    market_status: 简短描述当前盘面状态。
    reason: 升级或不升级的理由。
    """
    pass

def screener_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    symbol = state.symbol
    model_name = agent_config.get('screener_model', 'gpt-4o-mini')

    logger.info(f"--- [Node] Screener Agent: {model_name} processing (Forced Tool) ---")

    llm = ChatOpenAI(
        model=model_name,
        api_key=agent_config.get('screener_api_key') or agent_config.get('api_key'),
        base_url=agent_config.get('screener_api_base') or agent_config.get('api_base'),
        temperature=agent_config.get('screener_temperature', 0.2)
    )

    llm_with_tools = llm.bind_tools([submit_screener_decision], tool_choice="submit_screener_decision")

    try:
        response = llm_with_tools.invoke(state.messages)
        tool_call = response.tool_calls[0]
        args = tool_call['args']
        
        result_dict = {
            "confidence": args.get("confidence", 0),
            "should_escalate": args.get("should_escalate", False),
            "market_status": args.get("market_status", "Parsed from Tool"),
            "reason": args.get("reason", "")
        }
        
        # 保存 token 使用量
        usage = response.response_metadata.get("token_usage", {})
        if usage:
            database.save_token_usage(
                symbol=symbol,
                config_id=config_id,
                model=model_name,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0)
            )
            
        return state.model_copy(update={"screener_result": result_dict, "messages": state.messages + [response]})
    except Exception as e:
        logger.error(f"❌ [Screener Error]: {e}")
        return state.model_copy(update={"screener_result": {"confidence": 0, "should_escalate": False, "market_status": "Error", "reason": str(e)}})


def escalation_router(state: AgentState, config: RunnableConfig) -> str:
    configurable = config.get("configurable", {})
    agent_config = configurable.get("agent_config", {})
    threshold = int(agent_config.get("escalation_threshold", 60))

    res = state.screener_result
    if not res:
        logger.warning("⚠️ No screener result found, skipping escalation.")
        return "skip"

    confidence = res.get("confidence", 0)
    should_escalate = res.get("should_escalate", False)

    logger.info(f"🎯 [Screener Result]: Confidence: {confidence}, Escalate: {should_escalate}. Reason: {res.get('reason')}")

    if should_escalate and confidence >= threshold:
        logger.info(f"🚀 Escalating to Analyst model (Threshold {threshold} met)!")
        return "escalate"
    
    logger.info(f"💤 Skipping Analyst model (Threshold {threshold} not met).")
    return "skip"


def screener_finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """仅保留基础筛选记录到数据库"""
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    res = state.screener_result
    if res:
         # Save a very brief summary noting no action taken
         agent_name = agent_config.get('screener_model', 'Screener')
         summary_text = f"【筛选记录】自信度: {res.get('confidence')} | 需要升级: {res.get('should_escalate')}\n盘面状态: {res.get('market_status')}\n理由: {res.get('reason')}"
         strategy_logic = f"Screener: 波动较小或未满足阈值，不升级分析。"
         database.save_summary(state.symbol, agent_name, summary_text, strategy_logic, config_id=config_id, agent_type="SCREENER")
    
    return state


def analyst_start_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """如果升级，则获取全面的数据准备给大模型"""
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    symbol = state.symbol
    logger.info(f"--- [Node] Analyst Start: Fetching FULL data for {symbol} ---")

    market_tool = MarketTool(config_id=config_id)
    try:
        # 全量数据
        timeframes_to_fetch = ['15m', '1h', '4h', '1d', '1w']
        market_full = market_tool.get_market_analysis(symbol, mode='REAL', timeframes=timeframes_to_fetch)
        account_data = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id, config_id=config_id)
        from database import get_daily_summaries
        daily_history = get_daily_summaries(config_id, days=7)
    except Exception as e:
        logger.error(f"❌ [Analyst Data Fetch Error]: {e}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        daily_history = []
    
    now = datetime.now()
    try:
        balance = account_data.get('balance', 0)
        positions = account_data.get('real_positions', [])
        total_unrealized_pnl = sum([float(p.get('unrealized_pnl', 0)) for p in positions])
        
        if now.minute < 15:
            database.save_balance_snapshot(symbol, balance, total_unrealized_pnl)
            
        recent_trades = market_tool.fetch_recent_trades(symbol, limit=10)
        if recent_trades:
            database.save_trade_history(recent_trades)
    except Exception as e:
        logger.error(f"❌ Failed to save real-time stats: {e}")

    # 将状态更新为包含完整数据的表示
    updated_state = state.model_copy(update={
        "market_context": market_full,
        "account_context": account_data,
        "history_context": daily_history,
    })

    prompt = _get_analyst_prompt(agent_config, updated_state)
    screener_res = updated_state.screener_result
    screener_msg = f"这是第一层(Screener)快速浏览市场后给你的建议备忘（仅参考，不要盲从）:\n自信度:{screener_res.get('confidence')}\n状态:{screener_res.get('market_status')}\n理由:{screener_res.get('reason')}"
    
    return updated_state.model_copy(update={"messages": [HumanMessage(content=prompt), HumanMessage(content=screener_msg)]})


def analyst_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    symbol = state.symbol
    model_name = agent_config.get('analyst_model', 'gpt-4o')

    logger.info(f"--- [Node] Analyst Agent: {model_name} processing (REAL Tools) ---")

    tools = [open_position_real, close_position_real, cancel_orders_real]

    llm = ChatOpenAI(
        model=model_name,
        api_key=agent_config.get('analyst_api_key') or agent_config.get('api_key'),
        base_url=agent_config.get('analyst_api_base') or agent_config.get('api_base'),
        temperature=agent_config.get('analyst_temperature', 0.5),
    ).bind_tools(tools)

    messages = state.messages
    response = llm.invoke(messages)
    
    reasoning = response.additional_kwargs.get("reasoning_content") or response.response_metadata.get("reasoning_content")
    if reasoning and "<thinking>" not in response.content:
        response.content = f"<thinking>\n{reasoning}\n</thinking>\n\n{response.content}"

    try:
        usage = response.response_metadata.get("token_usage", {})
        if usage:
            database.save_token_usage(
                symbol=symbol,
                config_id=config_id,
                model=model_name,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0)
            )
    except Exception as usage_e:
        logger.warning(f"⚠️ [Analyst] Failed to save token usage: {usage_e}")

    return state.model_copy(update={"messages": state.messages + [response]})


def analyst_finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    agent_name = agent_config.get('analyst_model', 'Analyst')
    
    all_ai_messages = [msg for msg in state.messages if isinstance(msg, AIMessage) and msg.content]
    if all_ai_messages:
        sorted_msgs = sorted(all_ai_messages, key=lambda m: len(m.content), reverse=True)
        main_content = sorted_msgs[0].content 
        
        # 将 screener 结果和 analyst 结果合并存入数据库
        screener_res = state.screener_result
        if screener_res:
            main_content = f"**[Screener 引荐]**\n- Confidence: {screener_res.get('confidence')}\n- 盘面: {screener_res.get('market_status')}\n- 理由: {screener_res.get('reason')}\n\n**[Analyst 深度分析]**\n" + main_content
        
        strategy_logic = summarize_content(main_content, agent_config)
        
        processed_content = escape_markdown_special_chars(main_content)
        processed_strategy_logic = escape_markdown_special_chars(strategy_logic)
        
        database.save_summary(symbol, agent_name, processed_content, processed_strategy_logic, config_id=config_id, agent_type="ANALYST")

    return state

def should_continue(state: AgentState):
    last_message = state.messages[-1]
    if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "tools"
    return "finalize"


workflow = StateGraph(AgentState)
workflow.add_node("screener_start", screener_start_node)
workflow.add_node("screener_agent", screener_agent_node)
workflow.add_node("analyst_start", analyst_start_node)
workflow.add_node("analyst_agent", analyst_agent_node)
workflow.add_node("tools", tools_node) # Using existing tools_node
workflow.add_node("screener_finalize", screener_finalize_node)
workflow.add_node("analyst_finalize", analyst_finalize_node)

workflow.set_entry_point("screener_start")
workflow.add_edge("screener_start", "screener_agent")
workflow.add_conditional_edges("screener_agent", escalation_router, {
    "escalate": "analyst_start",
    "skip": "screener_finalize"
})

workflow.add_edge("analyst_start", "analyst_agent")
workflow.add_conditional_edges("analyst_agent", should_continue, {
    "tools": "tools",
    "finalize": "analyst_finalize"
})
workflow.add_edge("tools", "analyst_agent")
workflow.add_edge("screener_finalize", END)
workflow.add_edge("analyst_finalize", END)

app = workflow.compile(name='Multi Agent Flow')

def run_multi_agent_for_config(config: dict, human_message: str = None):
    config_id = config.get('config_id', 'unknown')
    symbol = config['symbol']
    initial_state = AgentState(
        symbol=symbol,
        messages=[],
        market_context={},
        account_context={},
        history_context=[],
        full_analysis="",
        human_message=human_message,
        screener_result=None
    )
    try:
        app.invoke(initial_state, config={"configurable": {"config_id": config_id, "agent_config": config}})
    except Exception as e:
        logger.error(f"❌ Critical Multi-Agent Graph Error for [{config_id}] {symbol}: {e}")
