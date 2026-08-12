import uuid
import json
import re
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


import pytz
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END

from backend.agent.agent_models import AgentState
from backend.agent.tool_registry import get_trade_tools_for_mode, run_trade_tool
from backend.utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, \
    format_market_data_to_text
from backend.utils.llm_utils import (
    LLMInvocationError,
    build_chat_model,
    extract_message_text,
    extract_reasoning_token_count,
    extract_reasoning_content,
    instruction_message,
    invoke_with_retry,
    sync_langsmith_environment,
)
from backend.utils.logger import setup_logger
from backend.utils.prompt_utils import resolve_prompt_file_content, resolve_prompt_template, render_prompt

import backend.database as database
from backend.database import (
    get_daily_summaries,
    get_short_memories,
    get_short_memory,
    get_recent_summary_logic,
    get_summary_logic_between,
    save_short_memory,
)
from backend.utils.market_data import MarketTool
from backend.utils.news_context import fetch_news_risk_context
from backend.config import config as global_config

TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))


def _emit_task_progress(configurable: dict, *, phase: str, message: str, **payload) -> None:
    callback = configurable.get("progress_callback")
    if not callable(callback):
        return
    try:
        callback({"phase": phase, "message": message, **payload})
    except Exception as exc:
        logger.warning("Task progress callback failed: %s", exc)


def resolve_market_timeframes(agent_config: dict | None = None) -> list[str]:
    agent_timeframes = [str(item).strip() for item in list((agent_config or {}).get('market_timeframes') or []) if str(item).strip()]
    if agent_timeframes:
        return agent_timeframes

    global_timeframes = [str(item).strip() for item in list(getattr(global_config, 'market_timeframes', None) or []) if str(item).strip()]
    if global_timeframes:
        return global_timeframes

    return ['15m', '30m', '1h', '4h', '1d', '1w', '1M']
TZ_US = pytz.timezone('America/New_York')
logger = setup_logger("AgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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

def summarize_content(content: str, agent_config: dict, summary_type: str = "strategy") -> str:
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
        llm = build_chat_model(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            thinking_enabled=summarizer_cfg.get("thinking_enabled"),
            reasoning_effort=summarizer_cfg.get("reasoning_effort"),
            compatibility_mode=summarizer_cfg.get("compatibility_mode"),
        )
        prompt = f"""请将以下交易分析内容压缩为一段简短的“策略逻辑思路”（150字以内），保留趋势情况、关键点位(支持阻力)和操作意图等等。
直接输出压缩后的文字，不要有任何前缀。
内容：
{content}
"""
        default_prompts = {
            "strategy": "请把以下单轮交易分析压缩成一段中文策略记忆，150字以内。保留趋势判断、关键价位、风险点、持仓/挂单意图和下一步动作。只输出总结文本。\n\n内容：\n{content}",
            "daily": "请把以下一整天的交易推理压缩成一段中文日内记忆，300字以内。保留趋势演变、关键价位、决策变化、执行动作和风险结论。只输出总结文本。\n\n内容：\n{content}",
            "short_memory": "请把以下最近一段时间的交易总结滚动压缩成中文短期记忆，400-600字。保留市场状态、连续决策变化、持仓/挂单变化、关键价位、已实现/未实现盈亏和风险提醒。只输出总结文本。\n\n内容：\n{content}",
        }
        prompt_text_key = {
            "strategy": "strategy_prompt",
            "daily": "daily_prompt",
            "short_memory": "short_memory_prompt",
        }.get(summary_type, "strategy_prompt")
        prompt_file_key = {
            "strategy": "strategy_prompt_file",
            "daily": "daily_prompt_file",
            "short_memory": "short_memory_prompt_file",
        }.get(summary_type, "strategy_prompt_file")
        prompt_template = str(summarizer_cfg.get(prompt_text_key) or "").strip()
        if not prompt_template:
            prompt_template = resolve_prompt_file_content(
                summarizer_cfg.get(prompt_file_key),
                PROJECT_ROOT,
                logger,
                fallback=default_prompts.get(summary_type, default_prompts["strategy"]),
            )
        prompt = render_prompt(prompt_template, content=content)

        response = invoke_with_retry(
            lambda: llm.invoke([HumanMessage(content=prompt)]),
            logger=logger,
            context=f"summarizer model={model} config_id={agent_config.get('config_id', 'summarizer')}",
        )
        
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

        return extract_message_text(response).strip()
    except Exception as e:
        logger.error(f"❌ [Summarizer Error]: {e}")
        if summary_type == "short_memory":
            return ""
        return content[:200] + "..."

def generate_manual_daily_summary(config_id: str, date_str: str) -> bool:
    """手动或通过调度器触发特定周期的每日总结汇总。"""
    from backend.database import get_pending_daily_summary_data, save_daily_summary
    from backend.config import config as global_config
    
    # 查找对应的 config
    all_configs = global_config.get_all_symbol_configs()
    target_config = next((c for c in all_configs if c['config_id'] == config_id), None)
    if not target_config:
        logger.error(f"Config ID {config_id} not found for manual summary.")
        return False
        
    try:
        rows = get_pending_daily_summary_data(config_id, date_str)
        if not rows:
            logger.info(f"No summary data found for {config_id} on {date_str}")
            return False
            
        combined = "\n".join(
            f"[{r['timestamp']}] {r['strategy_logic']}"
            for r in rows if r.get('strategy_logic')
        )
        if not combined.strip():
            return False
            
        summary_text = summarize_content(
            f"以下是 {date_str} 一整天的多轮交易分析逻辑，请汇总为一段200字以内的当日策略行情回顾，"
            f"保留关键趋势判断、核心点位和操作意图的演变过程：\n\n{combined}",
            target_config,
            summary_type="daily",
        )
        
        save_daily_summary(date_str, target_config.get('symbol', 'Unknown'), config_id, summary_text, len(rows))
        return True
    except Exception as e:
        logger.error(f"Failed to generate manual daily summary for {config_id}: {e}")
        return False


def get_short_memory_bucket(now_cn: datetime | None = None) -> tuple[datetime, datetime]:
    now_cn = now_cn or datetime.now(TZ_CN)
    bucket_hour = (now_cn.hour // 4) * 4
    bucket_start = now_cn.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
    return bucket_start, bucket_start + timedelta(hours=4)


def format_short_memory_text(config_id: str, limit: int = 1) -> str:
    memories = get_short_memories(config_id, limit=limit)
    if not memories:
        return "(No short-term memory yet)"

    entries = []
    for item in memories:
        market = item.get("market_summary") or ""
        entries.append(
            f"[updated={item.get('bucket_start')}] sources={item.get('source_count', 0)}\n"
            f"{market or '-'}"
        )
    return "\n\n".join(entries)


def format_short_memory_for_llm(config_id: str, limit: int = 1) -> str:
    memories = get_short_memories(config_id, limit=limit)
    entries = [
        str(item.get("market_summary") or "").strip()
        for item in memories
        if str(item.get("market_summary") or "").strip()
    ]
    return "\n\n".join(entries) if entries else "(No short-term memory yet)"


def _is_invalid_short_memory_summary(summary: str, source_input: str) -> bool:
    text = str(summary or "").strip()
    if not text:
        return True
    markers = (
        "Window:",
        "Symbol:",
        "Previous rolling memory:",
        "Previous short memory:",
        "Recent strategy summaries:",
        "Recent market/agent reasoning:",
    )
    if any(marker in text for marker in markers):
        return True
    return text.endswith("...") and source_input.startswith(text[:-3])


def generate_rolling_short_memory_for_config(
    config_id: str,
    agent_config: dict | None = None,
    now_cn: datetime | None = None,
    hours: int = 12,
    limit: int = 12,
) -> bool:
    now_cn = now_cn or datetime.now(TZ_CN)
    all_configs = global_config.get_all_symbol_configs()
    target_config = agent_config or next((c for c in all_configs if c.get("config_id") == config_id), None)
    if not target_config or not target_config.get("enabled", True):
        return False

    since_time = (now_cn - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = get_recent_summary_logic(config_id, since_time=since_time, limit=limit)
    if not rows:
        return False

    rows = list(reversed(rows))
    previous = format_short_memory_for_llm(config_id, limit=1)
    source_text = "\n".join(
        f"[{row.get('timestamp')}] {row.get('strategy_logic')}"
        for row in rows
        if row.get("strategy_logic")
    )
    memory_input = (
        f"Window: last {hours}h\n"
        f"Symbol: {target_config.get('symbol')}\n\n"
        f"Previous rolling memory:\n{previous}\n\n"
        f"Recent strategy summaries:\n{source_text}"
    )
    memory_summary = summarize_content(memory_input, target_config, summary_type="short_memory")
    if _is_invalid_short_memory_summary(memory_summary, memory_input):
        logger.warning(f"Skip saving invalid rolling short memory for {config_id}.")
        return False
    end_stamp = now_cn.strftime("%Y-%m-%d %H:%M:%S")
    save_short_memory(
        since_time,
        end_stamp,
        target_config.get("symbol", "Unknown"),
        config_id,
        memory_summary,
        "",
        len(rows),
    )
    return True


def generate_short_memory_for_config(config_id: str, now_cn: datetime | None = None) -> bool:
    all_configs = global_config.get_all_symbol_configs()
    target_config = next((c for c in all_configs if c.get("config_id") == config_id), None)
    if not target_config or not target_config.get("enabled", True):
        return False

    bucket_start, bucket_end = get_short_memory_bucket(now_cn)
    start_text = bucket_start.strftime("%Y-%m-%d %H:%M:%S")
    end_text = bucket_end.strftime("%Y-%m-%d %H:%M:%S")
    existing = get_short_memory(config_id, start_text)
    if existing and int(existing.get("source_count") or 0) > 0:
        return False

    summary_rows = get_summary_logic_between(config_id, start_text, end_text)
    source_count = len(summary_rows)

    previous = format_short_memory_for_llm(config_id, limit=1)
    if source_count <= 0:
        save_short_memory(
            start_text,
            end_text,
            target_config.get("symbol", "Unknown"),
            config_id,
            "No new analysis in this 4h window. Reuse previous context if relevant.",
            "",
            0,
        )
        return True

    market_text = "\n".join(
        f"[{row.get('timestamp')}] {row.get('strategy_logic')}"
        for row in summary_rows
        if row.get("strategy_logic")
    )
    memory_input = (
        f"Window: {start_text} - {end_text}\n"
        f"Symbol: {target_config.get('symbol')}\n\n"
        f"Recent market/agent reasoning:\n{market_text or 'No new market reasoning.'}\n\n"
        f"Previous short memory:\n{previous}"
    )
    memory_summary = summarize_content(memory_input, target_config, summary_type="short_memory")
    if _is_invalid_short_memory_summary(memory_summary, memory_input):
        logger.warning(f"Skip saving invalid short memory for {config_id}.")
        return False
    save_short_memory(
        start_text,
        end_text,
        target_config.get("symbol", "Unknown"),
        config_id,
        memory_summary,
        "",
        source_count,
    )
    return True

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
        timeframes_to_fetch = resolve_market_timeframes(agent_config)
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode, timeframes=timeframes_to_fetch)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec, agent_name=agent_name, config_id=config_id)
        news_context = fetch_news_risk_context(symbol)
        database.save_news_snapshot(symbol, config_id, news_context)
        daily_history = get_daily_summaries(config_id, days=7)
        short_memory_text = format_short_memory_for_llm(config_id, limit=1)

        logger.debug(f"📊 Market data fetched: {len(market_full.get('analysis', {}))} timeframes")
        logger.debug(f"💰 Account balance: {account_data.get('balance', 0)} USDT")
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        import traceback
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        market_full = {}
        news_context = {}
        account_data = {
            'balance': 0,
            'available_balance': 0,
            'real_open_orders': [],
            'mock_open_orders': [],
            'real_positions': []
        }
        daily_history = []
        short_memory_text = "(No short-term memory yet)"

    if is_real_exec:
        try:
            balance = account_data.get('balance', 0)
            positions = account_data.get('real_positions', [])
            total_unrealized_pnl = sum([float(p.get('unrealized_pnl', 0)) for p in positions])
            
            if now.minute < 15:
                database.save_balance_snapshot(symbol, balance, total_unrealized_pnl, config_id=config_id)
                
            recent_trades = market_tool.fetch_recent_trades(symbol, limit=10)
            if recent_trades:
                database.save_trade_history(recent_trades)
                database.sync_trade_position_history(config_id, symbol, recent_trades)
            database.sync_open_position_history(config_id, symbol, positions)
        except Exception as e:
            logger.error(f"❌ Failed to save real-time stats: {e}")

    balance = account_data.get('balance', 0)
    prompt_balance = account_data.get('available_balance', balance)
    raw_analysis = market_full.get("analysis", {})
    analysis_data = raw_analysis.get("15m") or next(iter(raw_analysis.values()), {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0

    logger.debug(f"📈 Extracted from 15m: price={current_price}, atr={atr_15m}")
    logger.debug(f"💳 Prompt balance (available): {prompt_balance} USDT | Snapshot balance (total): {balance} USDT")

    indicators_summary = {}
    configured_timeframes = resolve_market_timeframes(agent_config)
    timeframes = [tf for tf in configured_timeframes if tf in raw_analysis] or list(raw_analysis.keys())

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
            "smc": tf_data.get("smc", {}),
            "liquidity_sweep_ifvg": tf_data.get("liquidity_sweep_ifvg", {}),
            "volume_analysis": tf_data.get("volume_analysis", {}),
        }
        # VWAP 仅日内周期存在
        if tf_data.get("vwap") is not None:
            indicators_summary[tf]["vwap"] = tf_data["vwap"]

    market_context_llm = {
        "current_price": current_price,
        "atr_base": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "news_context": news_context,
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

    formatted_history_text = (
        "## Rolling Short-Term Memory\n"
        f"{short_memory_text}\n\n"
        "## Daily Memory\n"
        f"{formatted_history_text}"
    )

    next_run_time = calculate_next_run_time(agent_config, now_cn)

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    prompt_template = resolve_prompt_template(agent_config, trade_mode, PROJECT_ROOT, logger)
    leverage = global_config.get_leverage(config_id)

    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [
            {
                "id": o.get('order_id'),
                "side": o.get('side'),
                "pos_side": o.get('pos_side'),
                "type": o.get('type'),
                "price": o.get('price'),
                "amount": o.get('amount'),
            }
            for o in raw_orders
        ]
        orders_friendly_text = format_orders_to_agent_friendly(display_orders)
    else:
        raw_mock_orders = account_data.get('mock_open_orders', [])
        
        # 区分未成交的挂单和已入场的模拟持仓
        active_mock_orders = [o for o in raw_mock_orders if not int(o.get('is_filled', 0))]
        active_mock_positions = [o for o in raw_mock_orders if int(o.get('is_filled', 0))]
        
        display_mock_orders = [{"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'), "tp": o.get('take_profit'), "sl": o.get('stop_loss')} for o in active_mock_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_mock_orders)
        
        display_mock_positions = []
        for o in active_mock_positions:
            side_str = str(o.get('side')).upper()
            pos_side = "LONG" if "BUY" in side_str else "SHORT"
            amt = float(o.get('amount', 0))
            entry = float(o.get('price', 0))
            # 简单计算未实现盈亏
            pnl = (current_price - entry) * amt if pos_side == "LONG" else (entry - current_price) * amt
            display_mock_positions.append({
                "symbol": o.get('symbol', symbol),
                "side": pos_side,
                "amount": amt,
                "entry_price": entry,
                "unrealized_pnl": pnl
            })
        positions_text = format_positions_to_agent_friendly(display_mock_positions)

    system_prompt = render_prompt(
        prompt_template,
        model=agent_config.get('model'),
        symbol=symbol,
        leverage=leverage,
        current_time=current_time_str,
        next_run_time=next_run_time,
        current_price=current_price,
        atr_15m=atr_15m,
        balance=prompt_balance,
        positions_text=positions_text,
        orders_text=orders_friendly_text,
        formatted_market_data=formatted_market_data,
        short_memory_text=short_memory_text,
        history_text=formatted_history_text,
        dca_period_text=dca_period_text,
        dca_budget=dca_budget
    )

    prompt_role = agent_config.get("system_prompt_role", "system")
    instruction = instruction_message(system_prompt, prompt_role)
    if isinstance(instruction, HumanMessage) and state.human_message:
        messages = [HumanMessage(content=f"{system_prompt}\n\n## User request\n{state.human_message}")]
    else:
        messages = [instruction]
        if state.human_message:
            messages.append(HumanMessage(content=state.human_message))

    return state.model_copy(update={
        "market_context": market_full,
        "account_context": account_data,
        "history_context": daily_history,
        "messages": messages
    })

 

def agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    logger.info(f"--- [Node] Agent: {agent_config.get('model')} (Mode: {trade_mode}) ---")

    messages = list(state.messages)
    _emit_task_progress(
        configurable,
        phase="thinking",
        message="模型正在分析市场并规划工具调用",
        reasoning_content=_collect_agent_reasoning(messages),
    )

    try:
        kwargs = {}
        if agent_config.get('extra_body'):
            kwargs["extra_body"] = agent_config.get('extra_body')

        # 根据模式选择工具集
        tools = get_trade_tools_for_mode(trade_mode)
        logger.info(
            "[ToolRegistry] Bound tools for mode=%s: %s",
            trade_mode,
            [tool.name for tool in tools],
        )
        
        # if trade_mode == 'REAL':
        #     tools += [analyze_event_contract, format_event_contract_order]

        llm = build_chat_model(
            model=agent_config.get('model'),
            api_key=agent_config.get('api_key'),
            base_url=agent_config.get('api_base'),
            temperature=agent_config.get('temperature', 0.5),
            extra_body=kwargs.get("extra_body"),
            thinking_enabled=agent_config.get("thinking_enabled"),
            reasoning_effort=agent_config.get("reasoning_effort"),
            compatibility_mode=agent_config.get("compatibility_mode"),
        ).bind_tools(tools)

        response = invoke_with_retry(
            lambda: llm.invoke(messages),
            logger=logger,
            context=f"agent symbol={symbol} config_id={config_id} model={agent_config.get('model')}",
        )
        response_tool_calls = [
            {
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "args": call.get("args") or {},
                "status": "planned",
            }
            for call in (response.tool_calls or [])
        ]
        _emit_task_progress(
            configurable,
            phase="tool_planning" if response_tool_calls else "finalizing",
            message="模型已生成工具调用计划" if response_tool_calls else "模型分析完成，正在整理结果",
            reasoning_content=_collect_agent_reasoning(messages + [response]),
            tool_calls=response_tool_calls,
        )
        
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

    except LLMInvocationError as e:
        _emit_task_progress(configurable, phase="failed", message=str(e))
        logger.error(f"[LLM Error] ({symbol}) type={e.error_type}: {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})
    except Exception as e:
        _emit_task_progress(configurable, phase="failed", message=str(e))
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})

def small_agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    
    model_name = agent_config.get("model", "gpt-4o-mini")
    api_key = agent_config.get("api_key")
    api_base = agent_config.get("api_base")
    temperature = agent_config.get("temperature", 0.5)

    logger.info(f"--- [Node] Small Agent: {model_name} (Mode: {trade_mode}) ---")

    messages = []
    for msg in state.messages:
        messages.append(msg)

    try:
        kwargs = {}
        if agent_config.get('extra_body'):
            kwargs["extra_body"] = agent_config.get('extra_body')

        tools = get_trade_tools_for_mode(trade_mode)
        logger.info(
            "[ToolRegistry] Bound tools for small agent mode=%s: %s",
            trade_mode,
            [tool.name for tool in tools],
        )
        
        llm = build_chat_model(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            extra_body=kwargs.get("extra_body"),
            thinking_enabled=agent_config.get("thinking_enabled"),
            reasoning_effort=agent_config.get("reasoning_effort"),
            compatibility_mode=agent_config.get("compatibility_mode"),
        ).bind_tools(tools)

        response = invoke_with_retry(
            lambda: llm.invoke(messages),
            logger=logger,
            context=f"small-agent symbol={symbol} config_id={config_id} model={model_name}",
        )
        
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

        return state.model_copy(update={"messages": state.messages + [response], "active_agent": "MASTER"})

    except LLMInvocationError as e:
        logger.error(f"[Small Agent Error] ({symbol}) type={e.error_type}: {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})
    except Exception as e:
        logger.error(f"❌ [Small Agent Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})

def _collect_agent_reasoning(messages: list[BaseMessage]) -> str:
    sections: list[str] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        reasoning = extract_reasoning_content(message).strip()
        reasoning_tokens = extract_reasoning_token_count(message)
        if not reasoning and not reasoning_tokens:
            continue
        tool_names = [str(call.get("name") or "") for call in (message.tool_calls or []) if call.get("name")]
        label = f"### 推理阶段 {len(sections) + 1}"
        if tool_names:
            label += f" · 调用 {', '.join(tool_names)}"
        if reasoning:
            sections.append(f"{label}\n\n{reasoning}")
        else:
            sections.append(
                f"{label}\n\n模型使用了 {reasoning_tokens} 个推理 token，"
                "但上游接口没有返回可展示的思考摘要。"
            )
    return "\n\n---\n\n".join(sections)


def finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """合并 AI 消息的内容并保存到数据库。"""
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    agent_name = agent_config.get('model', 'Unknown')
    
    all_ai_messages = [
        (msg, extract_message_text(msg))
        for msg in state.messages
        if isinstance(msg, AIMessage) and extract_message_text(msg)
    ]
    
    full_content = ""
    if all_ai_messages:
        sorted_msgs = sorted(all_ai_messages, key=lambda item: len(item[1]), reverse=True)
        main_content = sorted_msgs[0][1]
        other_parts = [text for message, text in all_ai_messages if message is not sorted_msgs[0][0]]
        
        if other_parts:
            full_content = main_content + "\n\n---\n\n" + "\n\n".join(other_parts)
        else:
            full_content = main_content
    
    agent_type = "MASTER"
    final_full_content = full_content
    reasoning_content = _collect_agent_reasoning(state.messages)

    if final_full_content:
        # 汇总逻辑仅针对主要内容
        logic_source = full_content if full_content else final_full_content
        strategy_logic = summarize_content(logic_source, agent_config)
        
        try:
            database.save_summary(
                symbol,
                agent_name,
                final_full_content,
                strategy_logic,
                config_id=config_id,
                agent_type=agent_type,
                reasoning_content=reasoning_content,
            )
            try:
                generate_rolling_short_memory_for_config(
                    config_id,
                    agent_config=agent_config,
                    now_cn=datetime.now(TZ_CN),
                )
            except Exception as memory_exc:
                logger.warning(f"Failed to update rolling short memory for {config_id}: {memory_exc}")
            
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
                    logger.info(f"SPOT_DCA no order generated for {config_id}; summary saved without order log.")
        except Exception as e:
            logger.warning(f"⚠️ Save summary/DCA log failed: {e}")

    _emit_task_progress(
        configurable,
        phase="completed",
        message="分析与工具执行结果已保存",
        reasoning_content=reasoning_content,
    )

    return state

def should_continue(state: AgentState):
    last_message = state.messages[-1]
    if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
        return "tools"
    return "finalize"


def tools_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """Execute real tool_calls emitted by the model."""
    last_message = state.messages[-1]
    tool_calls = getattr(last_message, 'tool_calls', [])

    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    symbol = state.symbol

    tool_outputs = []
    progress_calls = [
        {
            "id": str(call.get("id") or ""),
            "name": str(call.get("name") or ""),
            "args": call.get("args") or {},
            "status": "pending",
        }
        for call in tool_calls
    ]
    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        args = tool_call.get("args", {})
        logger.info(
            "ToolNode dispatching real tool_call: name=%s config_id=%s symbol=%s",
            tool_name,
            config_id,
            symbol,
        )
        for item in progress_calls:
            if item["id"] == str(tool_call.get("id") or ""):
                item["status"] = "running"
        _emit_task_progress(
            configurable,
            phase="tool_running",
            message=f"正在执行工具 {tool_name}",
            reasoning_content=_collect_agent_reasoning(state.messages),
            tool_calls=progress_calls,
        )

        try:
            result = run_trade_tool(tool_name, args, config_id, symbol)
            tool_outputs.append(ToolMessage(tool_call_id=tool_call["id"], content=result))
            status = "completed"
        except Exception as e:
            logger.error("Error executing tool %s: %s", tool_name, e)
            tool_outputs.append(ToolMessage(tool_call_id=tool_call["id"], content=f"Error: {str(e)}"))
            status = "failed"
        for item in progress_calls:
            if item["id"] == str(tool_call.get("id") or ""):
                item["status"] = status

    _emit_task_progress(
        configurable,
        phase="thinking",
        message="工具执行完成，模型正在继续推理",
        reasoning_content=_collect_agent_reasoning(state.messages),
        tool_calls=progress_calls,
    )

    return state.model_copy(update={"messages": state.messages + tool_outputs})

# ==========================================
# 4. Graph Construction
# ==========================================

workflow = StateGraph(AgentState)
workflow.add_node("start", start_node)
workflow.add_node("agent", agent_node)
workflow.add_node("small_agent", small_agent_node)
workflow.add_node("tools", tools_node)
workflow.add_node("finalize", finalize_node)

workflow.set_entry_point("start")

def start_router(state: AgentState, config: RunnableConfig) -> str:
    return "agent"

workflow.add_conditional_edges("start", start_router, {
    "agent": "agent"
})

workflow.add_conditional_edges("agent", should_continue, {"tools": "tools", "finalize": "finalize"})
workflow.add_conditional_edges("small_agent", should_continue, {"tools": "tools", "finalize": "finalize"})
workflow.add_edge("tools", "agent")
workflow.add_edge("finalize", END)

app = workflow.compile(name='Crypto Agent')

def run_agent_for_config(config: dict, human_message: str = None, progress_callback=None):
    config_id = config.get('config_id', 'unknown')
    symbol = config['symbol']
    sync_langsmith_environment()
    initial_state = AgentState(
        symbol=symbol,
        messages=[],
        market_context={},
        account_context={},
        history_context=[],
        full_analysis="",
        human_message=human_message
    )
    try:
        app.invoke(
            initial_state,
            config={
                "configurable": {
                    "config_id": config_id,
                    "agent_config": config,
                    "progress_callback": progress_callback,
                }
            },
        )
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
