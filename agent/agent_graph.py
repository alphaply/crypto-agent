import uuid
import json
import re
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, AIMessage, ToolMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from agent.agent_models import AgentState
from agent.agent_tools import (
    open_position_real, close_position_real, cancel_orders_real,
    open_position_strategy, cancel_orders_strategy, open_position_spot_dca,
    analyze_event_contract, format_event_contract_order
)
from utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, \
    format_market_data_to_text, escape_markdown_special_chars
from utils.logger import setup_logger
from utils.prompt_utils import resolve_prompt_template, render_prompt

import database
from database import get_daily_summaries
from utils.market_data import MarketTool
from config import config as global_config

TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))
TZ_US = pytz.timezone('America/New_York')
logger = setup_logger("AgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def calculate_next_run_time(agent_config, now_cn):
    """计算该 agent 的下次运行时间（用于注入 Prompt）"""
    mode = agent_config.get('mode', 'STRATEGY').upper()

    if mode in ['REAL', 'STRATEGY']:
        default_interval = 60 if mode == 'STRATEGY' else 15
        interval = int(agent_config.get('run_interval', default_interval))
        if interval < 15:
            interval = 15
        minutes_since_midnight = now_cn.hour * 60 + now_cn.minute
        next_total_minutes = ((minutes_since_midnight // interval) + 1) * interval
        next_run = now_cn.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_total_minutes)
        return next_run.strftime('%H:%M')

    elif mode == 'SPOT_DCA':
        dca_time_str = agent_config.get('dca_time', '08:00')
        try:
            hour, minute = map(int, dca_time_str.split(':'))
        except Exception:
            hour, minute = 8, 0
        next_run = now_cn.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if agent_config.get('dca_freq', '1d') == '1w':
            target_weekday = int(agent_config.get('dca_weekday', 0))
            days_ahead = target_weekday - now_cn.weekday()
            if days_ahead < 0 or (days_ahead == 0 and now_cn > next_run):
                days_ahead += 7
            next_run += timedelta(days=days_ahead)
        else:
            if now_cn > next_run:
                next_run += timedelta(days=1)
        return next_run.strftime('%m-%d %H:%M')

    return "N/A"

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
        prompt = f"""请将以下交易分析内容压缩为一段简短的“策略逻辑思路”（150字以内），保留趋势情况、关键点位(支持阻力)和操作意图等等。
直接输出压缩后的文字，不要有任何前缀。
内容：
{content}
"""
        response = llm.invoke([SystemMessage(content=prompt)])
        
        # 记录 Token 使用情况
        try:
            usage = response.response_metadata.get("token_usage", {})
            if usage:
                database.save_token_usage(
                    symbol=agent_config.get("symbol", "System"),
                    config_id=agent_config.get("config_id", "summarizer"),
                    model=model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0)
                )
        except Exception as usage_e:
            logger.warning(f"⚠️ [Summarizer] Failed to save token usage: {usage_e}")

        return response.content.strip()
    except Exception as e:
        logger.error(f"❌ [Summarizer Error]: {e}")
        return content[:200] + "..."

# ==========================================
# 2. Nodes
# ==========================================

def start_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    now_cn = datetime.now(TZ_CN)
    now_us = datetime.now(TZ_US)
    now = now_cn  # Maintain backward compatibility for snapshot logic
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    week_map_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    current_time_str = (
        f"北京: {now_cn.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now_cn.weekday()]}) | "
        f"美东: {now_us.strftime('%Y-%m-%d %H:%M:%S')} ({week_map_en[now_us.weekday()]})"
    )

    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode in ['REAL', 'SPOT_DCA'])
    agent_name = config_id

    # 提取定投周期与预算逻辑 (SPOT_DCA 专属)
    dca_period_text = "每天"
    dca_budget = agent_config.get('dca_amount') or agent_config.get('dca_budget') or 100
    
    if trade_mode == 'SPOT_DCA':
        dca_freq = agent_config.get('dca_freq', '1d').lower()
        dca_time = agent_config.get('dca_time', '08:00')
        dca_weekday = agent_config.get('dca_weekday', 0)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        
        if dca_freq == '1w':
            try:
                wd_idx = int(dca_weekday)
                wd_str = weekdays[wd_idx % 7]
            except:
                wd_str = "指定日期"
            dca_period_text = f"每周 ({wd_str}) {dca_time}"
        else:
            dca_period_text = f"每天 {dca_time}"

    market_tool = MarketTool(config_id=config_id)
    logger.info(f"--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        timeframes_to_fetch = ['5m', '15m', '1h', '4h', '1d', '1w']
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode, timeframes=timeframes_to_fetch)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec, agent_name=agent_name, config_id=config_id)
        daily_history = get_daily_summaries(config_id, days=7)

        logger.debug(f"📊 Market data fetched: {len(market_full.get('analysis', {}))} timeframes")
        logger.debug(f"💰 Account balance: {account_data.get('balance', 0)} USDT")
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        import traceback
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        daily_history = []

    if is_real_exec:
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

    balance = account_data.get('balance', 0)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0

    logger.debug(f"📈 Extracted from 15m: price={current_price}, atr={atr_15m}")

    indicators_summary = {}
    if trade_mode == 'STRATEGY':
        timeframes = ['1h', '4h', '1d', '1w']
    elif trade_mode == 'SPOT_DCA':
        timeframes = ['1h', '4h', '1d', '1w']
    else:
        timeframes = ['15m', '1h', '4h', '1d']

    raw_analysis = market_full.get("analysis", {})
    logger.debug(f"🔍 Available timeframes in raw_analysis: {list(raw_analysis.keys())}")

    for tf in timeframes:
        if tf not in raw_analysis:
            logger.warning(f"⚠️ Timeframe {tf} not found in raw_analysis")
            continue
        tf_data = raw_analysis[tf]
        indicators_summary[tf] = {
            "price": tf_data.get("price"),
            "trend": tf_data.get("trend", {}),
            "recent_opens": tf_data.get("recent_opens", []),
            "recent_closes": tf_data.get("recent_closes", []),
            "recent_highs": tf_data.get("recent_highs", []),
            "recent_lows": tf_data.get("recent_lows", []),
            "ema": tf_data.get("ema"),
            "rsi_analysis": tf_data.get("rsi_analysis", {}),
            "atr": tf_data.get("atr"),
            "macd": tf_data.get("macd"),
            "bollinger": tf_data.get("bollinger"),
            "vp": tf_data.get("vp", {}),
            "volume_analysis": tf_data.get("volume_analysis", {}),
        }
        # VWAP 仅日内周期存在
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
            date_str = ds.get('date', '未知日期')
            summary = ds.get('summary', '')
            count = ds.get('source_count', 0)
            if summary:
                history_entries.append(f"  [{date_str}] ({count}轮分析) {summary}")
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    next_run_time = calculate_next_run_time(agent_config, now_cn)

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    prompt_template = resolve_prompt_template(agent_config, trade_mode, PROJECT_ROOT, logger)
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
        model=agent_config.get('model'),
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
        dca_period_text=dca_period_text,
        dca_budget=dca_budget
    )

    messages = [HumanMessage(content=system_prompt)]
    if state.human_message:
        messages.append(HumanMessage(content=state.human_message))

    return state.model_copy(update={
        "market_context": market_full,
        "account_context": account_data,
        "history_context": daily_history,
        "messages": messages
    })

from langchain_core.tools import tool

@tool
def submit_screening_decision(decision: Literal["MASTER", "SMALL", "SKIP"], reason: str, market_status: str, analysis: str, prediction: str):
    """
    提交初筛决策。
    decision: 
        - "MASTER": 发现重要机会或风险，需要最强大的 Master 模型进行深度分析并执行交易。
        - "SMALL": 盘面波动较小或逻辑简单，交给 Small 模型处理（策略模式下 Small 模型可交易，实盘下仅做记录）。
        - "SKIP": 盘面无意义或无需任何操作，直接结束。
    reason: 做出该决策的详细理由，用于辅助判断。
    market_status: 简短描述当前盘面状态。
    analysis: 对当前行情的分析。
    prediction: 对未来走势的预测。
    """
    pass

def screener_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    screener_cfg = agent_config.get("screener", {})
    model_name = (screener_cfg.get("model") or os.getenv("GLOBAL_SCREENER_MODEL") or "gpt-4o-mini")
    api_key = (screener_cfg.get("api_key") or agent_config.get("api_key"))
    api_base = (screener_cfg.get("api_base") or agent_config.get("api_base"))
    
    logger.info(f"--- [Node] Router: {model_name} judging for {symbol} ---")

    now_cn = datetime.now(TZ_CN)
    current_time_str = now_cn.strftime('%Y-%m-%d %H:%M:%S')
    next_run_time = calculate_next_run_time(agent_config, now_cn)
    
    account_data = state.account_context
    balance = account_data.get('balance', 0)
    # 获取完整行情上下文
    raw_analysis = state.market_context.get("analysis", {})
    formatted_market_data = format_market_data_to_text({
        "current_price": raw_analysis.get("15m", {}).get("price", 0),
        "technical_indicators": {tf: data for tf, data in raw_analysis.items() if tf in ['15m', '1h', '4h', '1d']}
    })
    
    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    orders_text = format_orders_to_agent_friendly(account_data.get('real_open_orders', []))

    # 强制包含所有要素的内置 Prompt
    template = """你是一个加密货币交易路由网关（Router）。
你的唯一职责是：根据当前【行情数据】和【持仓状态】，决定是由“大模型（MASTER）”还是“小模型（SMALL）”来处理本轮逻辑。

### 核心上下文
- 币种: {symbol}
- 时间: {current_time}
- 下次运行: {next_run_time}
- 账户余额: {balance} USDT
- 当前持仓:
{positions_text}
- 挂单情况:
{orders_text}

### 市场行情
{formatted_market_data}

### 决策准则
1. **MASTER (大模型)**: 
   - 发现明显的趋势突破、反转信号或高确定性的交易机会。
   - 账户持仓出现风险，需要复杂的平仓或调仓逻辑。
   - 市场出现剧烈波动。
2. **SMALL (小模型)**: 
   - 市场处于震荡区间，只需进行常规维护。
   - 逻辑简单，小模型足以应对。
   - (注意：在策略模式下，SMALL 依然可以模拟开单)。
3. **SKIP (跳过)**:
   - 盘面完全没有交易价值，无需任何进一步分析。

请必须调用 submit_screening_decision 提交你的选择。"""

    screener_prompt = render_prompt(
        template,
        symbol=symbol,
        current_time=current_time_str,
        next_run_time=next_run_time,
        balance=balance,
        positions_text=positions_text,
        orders_text=orders_text,
        formatted_market_data=formatted_market_data
    )

    llm = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=api_base,
        temperature=0.1
    ).bind_tools([submit_screening_decision], tool_choice="submit_screening_decision")

    try:
        response = llm.invoke([HumanMessage(content=screener_prompt)])
        args = response.tool_calls[0]['args']
        
        usage = response.response_metadata.get("token_usage", {})
        if usage:
            database.save_token_usage(symbol, config_id, model_name, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

        return state.model_copy(update={
            "screener_result": args, 
            "messages": state.messages + [response]
        })
    except Exception as e:
        logger.error(f"❌ [Router Error]: {e}")
        return state.model_copy(update={"screener_result": {"decision": "MASTER", "reason": f"Router error: {e}"}})

def screening_router(state: AgentState, config: RunnableConfig) -> str:
    res = state.screener_result or {}
    decision = res.get("decision", "MASTER")
    trade_mode = config.get("configurable", {}).get("agent_config", {}).get("mode", "STRATEGY").upper()

    logger.info(f"🎯 [Router Decision] Selected: {decision}, Reason: {res.get('reason')}")

    if decision == "MASTER":
        return "continue"
    
    if decision == "SMALL":
        # 只有在策略模式下，小模型才去执行交易节点
        return "small_agent" if trade_mode == "STRATEGY" else "skip"
    
    return "skip"

def agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    model_name = agent_config.get('model', '').lower()
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    logger.info(f"--- [Node] Agent: {agent_config.get('model')} (Mode: {trade_mode}) ---")

    messages = []
    is_deepseek = "deepseek" in model_name or "r1" in model_name
    
    for msg in state.messages:
        if isinstance(msg, AIMessage) and is_deepseek:
            if getattr(msg, "tool_calls", None) or "reasoning_content" in msg.additional_kwargs or msg.response_metadata.get("reasoning_content"):
                msg.additional_kwargs["reasoning_content"] = None
        messages.append(msg)

    try:
        kwargs = {}
        if agent_config.get('extra_body'):
            kwargs["extra_body"] = agent_config.get('extra_body')

        # 根据模式选择工具集
        if trade_mode == 'REAL':
            tools = [open_position_real, close_position_real, cancel_orders_real]
        elif trade_mode == 'SPOT_DCA':
            tools = [open_position_spot_dca, cancel_orders_real]
        else:
            tools = [open_position_strategy, cancel_orders_strategy]
        
        # if trade_mode == 'REAL':
        #     tools += [analyze_event_contract, format_event_contract_order]

        llm = ChatOpenAI(
            model=agent_config.get('model'),
            api_key=agent_config.get('api_key'),
            base_url=agent_config.get('api_base'),
            temperature=agent_config.get('temperature', 0.5),
            model_kwargs=kwargs
        ).bind_tools(tools)

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
                    model=agent_config.get('model'),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0)
                )
        except Exception as usage_e:
            logger.warning(f"⚠️ [Agent] Failed to save token usage: {usage_e}")

        return state.model_copy(update={"messages": state.messages + [response], "active_agent": "MASTER"})

    except Exception as e:
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})

def small_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    
    screener_cfg = agent_config.get("screener", {})
    model_name = (screener_cfg.get("model") or os.getenv("GLOBAL_SCREENER_MODEL") or agent_config.get("model", "gpt-4o-mini"))
    api_key = (screener_cfg.get("api_key") or agent_config.get("api_key"))
    api_base = (screener_cfg.get("api_base") or agent_config.get("api_base"))
    temperature = screener_cfg.get("temperature", 0.5)

    logger.info(f"--- [Node] Small Agent: {model_name} (Mode: {trade_mode}) ---")

    messages = []
    for msg in state.messages:
        messages.append(msg)

    try:
        kwargs = {}
        if screener_cfg.get('extra_body'):
            kwargs["extra_body"] = screener_cfg.get('extra_body')

        tools = [open_position_strategy, cancel_orders_strategy]
        
        llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            model_kwargs=kwargs
        ).bind_tools(tools)

        response = llm.invoke(messages)
        
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
            logger.warning(f"⚠️ [Small Agent] Failed to save token usage: {usage_e}")

        return state.model_copy(update={"messages": state.messages + [response], "active_agent": "SCREENER"})

    except Exception as e:
        logger.error(f"❌ [Small Agent Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "SCREENER"})

def tools_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """通用的工具执行节点。"""
    last_message = state.messages[-1]
    tool_calls = getattr(last_message, 'tool_calls', [])
    
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    
    symbol = state.symbol
    
    tool_outputs = []
    # 动态映射可用工具
    available_tools_map = {
        "open_position_real": open_position_real,
        "close_position_real": close_position_real,
        "cancel_orders_real": cancel_orders_real,
        "open_position_strategy": open_position_strategy,
        "cancel_orders_strategy": cancel_orders_strategy,
        "open_position_spot_dca": open_position_spot_dca,
        "analyze_event_contract": analyze_event_contract,
        "format_event_contract_order": format_event_contract_order
    }
    
    for tool_call in tool_calls:
        tool_name = tool_call['name']
        args = tool_call['args']
        logger.info(f"🛠️ ToolNode Dispatching: {tool_name}")
        
        if tool_name in available_tools_map:
            tool_obj = available_tools_map[tool_name]
            args['config_id'] = config_id
            args['symbol'] = symbol
            
            try:
                result = tool_obj.func(**args)
                tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=str(result)))
            except Exception as e:
                logger.error(f"❌ Error executing tool {tool_name}: {e}")
                tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=f"Error: {str(e)}"))
        else:
            tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=f"Error: Tool '{tool_name}' not found."))
            
    return state.model_copy(update={"messages": state.messages + tool_outputs})

def finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """合并 AI 消息的内容并保存到数据库。"""
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    agent_name = agent_config.get('model', 'Unknown')
    
    all_ai_messages = [msg for msg in state.messages if isinstance(msg, AIMessage) and msg.content]
    
    full_content = ""
    if all_ai_messages:
        sorted_msgs = sorted(all_ai_messages, key=lambda m: len(m.content), reverse=True)
        main_content = sorted_msgs[0].content 
        other_parts = [m.content for m in all_ai_messages if m != sorted_msgs[0]]
        
        if other_parts:
            full_content = main_content + "\n\n---\n\n" + "\n\n".join(other_parts)
        else:
            full_content = main_content
    
    # 内容合并逻辑
    agent_type = None
    final_full_content = ""
    
    # 1. 提取初筛结论 (如果有)
    screener_prelude = ""
    if state.screener_result:
        res = state.screener_result
        screener_prelude = f"### 🔍 初筛初步研判\n- **盘面状态**: {res.get('market_status', 'N/A')}\n- **机会置信度**: {res.get('confidence', 0)}%\n- **初筛分析**: {res.get('analysis', '')}\n- **短期预测**: {res.get('prediction', '')}\n\n---\n"

    # 2. 构造最终展示内容
    if full_content:
        # 如果有 AI 消息内容，说明执行了 agent 或 small_agent
        if getattr(state, "active_agent", "MASTER") == "SCREENER":
            final_full_content = screener_prelude + f"### 🔍 策略模式初筛模型执行\n{full_content}\n\n> 💡 本轮由初筛小模型完成动作。"
            agent_name = (agent_config.get("screener", {}).get("model") or "Screener")
            agent_type = "SCREENER"
        else:
            final_full_content = screener_prelude + f"### 🧠 深度决策分析\n{full_content}"
            agent_type = "MASTER"
            agent_name = agent_config.get('model', 'Unknown')
    elif state.screener_result:
        # 如果跳过了大模型，仅显示初筛
        res = state.screener_result
        final_full_content = f"### 🔍 初筛快速分析\n{res.get('analysis', '')}\n\n### 📈 走势预测\n{res.get('prediction', '')}\n\n> 💡 本轮分析由初筛模型完成，未触发深度分析或动作。判断理由：{res.get('reason', '')}"
        agent_name = (agent_config.get("screener", {}).get("model") or "Screener")
        agent_type = "SCREENER"

    if final_full_content:
        # 汇总逻辑仅针对主要内容
        logic_source = full_content if full_content else final_full_content
        strategy_logic = summarize_content(logic_source, agent_config)
        
        processed_content = escape_markdown_special_chars(final_full_content)
        processed_strategy_logic = escape_markdown_special_chars(strategy_logic)
        
        try:
            database.save_summary(symbol, agent_name, processed_content, processed_strategy_logic, config_id=config_id, agent_type=agent_type)
            
            # 针对 SPOT_DCA 模式的增强日志：如果没有任何下单动作，存入一条 NO_ACTION 记录
            trade_mode = agent_config.get('mode', 'STRATEGY').upper()
            if trade_mode == 'SPOT_DCA':
                # 检查是否执行了 open_position_spot_dca 工具
                has_dca_order = False
                for msg in state.messages:
                    if isinstance(msg, ToolMessage):
                        if "open_position_spot_dca" in str(getattr(msg, "name", "")):
                            has_dca_order = True
                            break
                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        if any(tc['name'] == 'open_position_spot_dca' for tc in msg.tool_calls):
                            has_dca_order = True
                            break
                
                if not has_dca_order:
                    wait_reason = f"💤 本轮未触发挂单条件。逻辑摘要：{strategy_logic}"
                    database.save_order_log(
                        f"DCA-WAIT-{int(datetime.now().timestamp())}",
                        symbol,
                        agent_name,
                        'WAIT',
                        0, 0, 0,
                        wait_reason,
                        trade_mode="SPOT_DCA",
                        config_id=config_id
                    )
        except Exception as e:
            logger.warning(f"⚠️ Save summary/DCA log failed: {e}")

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
workflow.add_node("screener", screener_node)
workflow.add_node("agent", agent_node)
workflow.add_node("small_agent", small_agent_node)
workflow.add_node("tools", tools_node)
workflow.add_node("finalize", finalize_node)

workflow.set_entry_point("start")

def start_router(state: AgentState, config: RunnableConfig) -> str:
    configurable = config.get("configurable", {})
    agent_config = configurable.get("agent_config", {})
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    
    if trade_mode in ['REAL', 'STRATEGY'] and agent_config.get('enable_screening', False):
        return "screener"
    return "agent"

workflow.add_conditional_edges("start", start_router, {
    "screener": "screener",
    "agent": "agent"
})

workflow.add_conditional_edges("screener", screening_router, {
    "continue": "agent",
    "small_agent": "small_agent",
    "skip": "finalize"
})

workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
workflow.add_conditional_edges("small_agent", should_continue, {"tools": "tools", "finalize": "finalize"})
workflow.add_edge("tools", "agent")
workflow.add_edge("finalize", END)

app = workflow.compile(name='Crypto Agent')

def run_agent_for_config(config: dict, human_message: str = None):
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
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
