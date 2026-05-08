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
from langgraph.graph import StateGraph, END

from agent.agent_models import AgentState
from agent.agent_tools import (
    open_position_real, close_position_real, cancel_orders_real,
    open_position_strategy, cancel_orders_strategy, close_position_strategy, open_position_spot_dca,
    analyze_event_contract, format_event_contract_order
)
from utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, \
    format_market_data_to_text, escape_markdown_special_chars
from utils.llm_utils import LLMInvocationError, build_chat_openai, invoke_with_retry
from utils.logger import setup_logger
from utils.prompt_utils import resolve_prompt_template, render_prompt

import database
from database import get_daily_summaries, get_recent_short_memories
from utils.market_data import MarketTool
from config import config as global_config

TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))
TZ_US = pytz.timezone('America/New_York')
logger = setup_logger("AgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_DAYS = 5
DEFAULT_DAILY_SUMMARY_PROMPT_FILE = "daily_summary.txt"
DEFAULT_STRATEGY_SUMMARY_PROMPT_FILE = "strategy_summary.txt"
DEFAULT_SHORT_MEMORY_PROMPT_FILE = "short_memory.txt"


def _read_prompt_file(prompt_file: str) -> str:
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        return ""

    try:
        file_path = Path(prompt_file.strip())
        if not file_path.is_absolute():
            candidate = PROJECT_ROOT / file_path
            if not candidate.exists():
                candidate = PROJECT_ROOT / "agent" / "prompts" / file_path
            file_path = candidate

        if file_path.exists():
            content = file_path.read_text(encoding="utf-8").strip()
            if content:
                logger.info(f"Using summary prompt file: {file_path}")
                return content
            logger.warning(f"Summary prompt file is empty: {file_path}")
        else:
            logger.warning(f"Summary prompt file does not exist: {file_path}")
    except Exception as e:
        logger.warning(f"Failed to load summary prompt file: {e}")

    return ""


def _resolve_daily_summary_prompt(agent_config: dict) -> str:
    summarizer_cfg = agent_config.get("summarizer", {})
    prompt_file = (
        summarizer_cfg.get("daily_prompt_file")
        or summarizer_cfg.get("prompt_file")
        or agent_config.get("daily_summary_prompt_file")
        or os.getenv("DAILY_SUMMARY_PROMPT_FILE")
        or DEFAULT_DAILY_SUMMARY_PROMPT_FILE
    )
    return _read_prompt_file(prompt_file)


def _resolve_strategy_summary_prompt(agent_config: dict) -> str:
    summarizer_cfg = agent_config.get("summarizer", {})
    prompt_file = (
        summarizer_cfg.get("strategy_prompt_file")
        or os.getenv("STRATEGY_SUMMARY_PROMPT_FILE")
        or DEFAULT_STRATEGY_SUMMARY_PROMPT_FILE
    )
    return _read_prompt_file(prompt_file)


def _resolve_short_memory_prompt(agent_config: dict) -> str:
    summarizer_cfg = agent_config.get("summarizer", {})
    prompt_file = (
        summarizer_cfg.get("short_memory_prompt_file")
        or os.getenv("SHORT_MEMORY_PROMPT_FILE")
        or DEFAULT_SHORT_MEMORY_PROMPT_FILE
    )
    return _read_prompt_file(prompt_file)


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

def summarize_content(content: str, agent_config: dict, prompt_template: str = "", prompt_context: dict = None) -> str:
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
        llm = build_chat_openai(
            model=model,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
        )
        prompt = render_prompt(
            prompt_template,
            content=content,
            **(prompt_context or {}),
        ) if prompt_template else f"""请将以下交易分析内容压缩为一段“市场状态复盘”（150字以内）。
只保留客观市场结构，不保留具体做多/做空偏好，不延续旧挂单意图，不输出“继续低吸/继续加仓/维持原策略”等惯性表述。

必须包含：
1. 主导周期趋势：1d/4h/1h 哪个方向占优，是趋势还是震荡。
2. 关键失效位：多头失效位、空头失效位。
3. 当前市场类型：趋势延续 / 趋势反转 / 区间震荡 / 高波动不确定。
4. 若存在频繁补仓、逆势、追高、被洗盘风险，必须明确记录为风险提醒。

禁止包含：
- “继续低吸”“继续加多”“支撑挂多为主”等延续性交易建议；
- 未经当前数据重新确认的方向偏好；
- 任何鼓励补仓摊平亏损的措辞。

直接输出压缩后的文字，不要有任何前缀。
内容：
{content}
"""
        response = invoke_with_retry(
            lambda: llm.invoke([SystemMessage(content=prompt)]),
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

        return response.content.strip()
    except Exception as e:
        logger.error(f"❌ [Summarizer Error]: {e}")
        return content[:200] + "..."

def generate_manual_daily_summary(config_id: str, date_str: str) -> bool:
    """Generate the daily summary with a prompt template editable from the admin UI."""
    from database import get_pending_daily_summary_data, save_daily_summary
    from config import config as global_config

    all_configs = global_config.get_all_symbol_configs()
    target_config = next((c for c in all_configs if c["config_id"] == config_id), None)
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
            for r in rows if r.get("strategy_logic")
        )
        if not combined.strip():
            return False

        daily_prompt_template = _resolve_daily_summary_prompt(target_config)
        summary_text = summarize_content(
            combined,
            target_config,
            prompt_template=daily_prompt_template,
            prompt_context={
                "date": date_str,
                "symbol": target_config.get("symbol", "Unknown"),
                "config_id": config_id,
                "source_count": len(rows),
            },
        )

        save_daily_summary(date_str, target_config.get("symbol", "Unknown"), config_id, summary_text, len(rows))
        return True
    except Exception as e:
        logger.error(f"Failed to generate manual daily summary for {config_id}: {e}")
        return False


def _format_position_events_for_memory(events):
    lines = []
    for event in events:
        ts = event.get("close_time") or event.get("open_time") or event.get("created_at") or ""
        parts = [
            f"[{ts}]",
            str(event.get("event_type") or ""),
            str(event.get("side") or ""),
            f"amount={event.get('amount', 0)}",
            f"entry={event.get('entry_price', 0)}",
        ]
        if event.get("close_price"):
            parts.append(f"close={event.get('close_price')}")
        if event.get("realized_pnl") is not None:
            parts.append(f"pnl={event.get('realized_pnl')}")
        if event.get("order_id"):
            parts.append(f"order={event.get('order_id')}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_short_memory_text(memories):
    if not memories:
        return "(暂无短期记忆)"
    entries = []
    for item in memories:
        header = (
            f"[{item.get('bucket_start')} ~ {item.get('bucket_end')}] "
            f"analysis={item.get('source_summary_count', 0)}, "
            f"positions={item.get('source_position_count', 0)}"
        )
        entries.append(
            f"{header}\n"
            f"市场: {item.get('market_summary') or '无'}\n"
            f"仓位: {item.get('position_summary') or '无'}"
        )
    return "\n\n".join(entries)


def generate_short_memory_for_config(config_id: str, bucket_start: str, bucket_end: str) -> bool:
    from database import (
        get_pending_short_memory_data,
        get_recent_short_memories,
        get_short_memory,
        save_short_memory,
    )

    if get_short_memory(config_id, bucket_start):
        return True

    target_config = global_config.get_config_by_id(config_id)
    if not target_config:
        logger.error(f"Config ID {config_id} not found for short memory.")
        return False
    if not target_config.get("enabled", True):
        logger.info(f"Skip short memory for disabled config: {config_id}")
        return False

    data = get_pending_short_memory_data(config_id, bucket_start, bucket_end)
    summary_rows = data.get("summaries", [])
    position_rows = data.get("positions", [])

    if not summary_rows and not position_rows:
        previous = get_recent_short_memories(config_id, limit=1)
        if previous:
            save_short_memory(
                bucket_start,
                bucket_end,
                target_config.get("symbol", "Unknown"),
                config_id,
                f"No new analysis in this 4h bucket. Previous market memory still applies: {previous[0].get('market_summary', '')}",
                f"No new position events in this 4h bucket. Previous position memory still applies: {previous[0].get('position_summary', '')}",
                0,
                0,
                fingerprint="no-change",
            )
            return True
        save_short_memory(
            bucket_start,
            bucket_end,
            target_config.get("symbol", "Unknown"),
            config_id,
            "No new analysis in this 4h bucket.",
            "No new position events in this 4h bucket.",
            0,
            0,
            fingerprint="empty",
        )
        return True

    analysis_text = "\n".join(
        f"[{row.get('timestamp')}] {row.get('strategy_logic') or row.get('content') or ''}"
        for row in summary_rows
    )
    position_text = _format_position_events_for_memory(position_rows)
    content = (
        f"Window: {bucket_start} ~ {bucket_end}\n"
        f"Symbol: {target_config.get('symbol', 'Unknown')}\n"
        f"Config: {config_id}\n\n"
        f"Recent market analysis:\n{analysis_text or '(none)'}\n\n"
        f"Recent position events:\n{position_text or '(none)'}"
    )

    prompt_template = _resolve_short_memory_prompt(target_config)
    summary_text = summarize_content(
        content,
        target_config,
        prompt_template=prompt_template,
        prompt_context={
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "symbol": target_config.get("symbol", "Unknown"),
            "config_id": config_id,
            "source_summary_count": len(summary_rows),
            "source_position_count": len(position_rows),
        },
    )

    parts = re.split(r"\n\s*---+\s*\n", summary_text, maxsplit=1)
    if len(parts) == 2:
        market_summary, position_summary = parts[0].strip(), parts[1].strip()
    else:
        market_summary = summary_text.strip()
        position_summary = "See market summary; no separate position section was returned."

    fingerprint = f"summaries:{len(summary_rows)}|positions:{len(position_rows)}"
    save_short_memory(
        bucket_start,
        bucket_end,
        target_config.get("symbol", "Unknown"),
        config_id,
        market_summary,
        position_summary,
        len(summary_rows),
        len(position_rows),
        fingerprint=fingerprint,
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
        timeframes_to_fetch = ['5m', '15m', '1h', '4h', '1d', '1w']
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode, timeframes=timeframes_to_fetch)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec, agent_name=agent_name, config_id=config_id)
        history_days = int(agent_config.get('history_days', DEFAULT_HISTORY_DAYS))
        daily_history = get_daily_summaries(config_id, days=history_days)
        short_memory_limit = int(agent_config.get("short_memory_limit", 2) or 2)
        short_memories = get_recent_short_memories(config_id, limit=short_memory_limit)

        logger.debug(f"📊 Market data fetched: {len(market_full.get('analysis', {}))} timeframes")
        logger.debug(f"💰 Account balance: {account_data.get('balance', 0)} USDT")
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        import traceback
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        market_full = {}
        account_data = {
            'balance': 0,
            'available_balance': 0,
            'real_open_orders': [],
            'mock_open_orders': [],
            'real_positions': []
        }
        daily_history = []
        short_memories = []

    if is_real_exec:
        try:
            balance = account_data.get('balance', 0)
            positions = account_data.get('real_positions', [])
            total_unrealized_pnl = sum([float(p.get('unrealized_pnl', 0)) for p in positions])
            
            if now.minute < 15:
                database.save_balance_snapshot(symbol, balance, total_unrealized_pnl)
                
            recent_trades = market_tool.fetch_recent_trades(symbol, limit=10)
            if recent_trades:
                database.save_trade_history(recent_trades, config_id=config_id)
            database.sync_real_position_history(config_id, symbol, positions=positions, trades=recent_trades or [])
        except Exception as e:
            logger.error(f"❌ Failed to save real-time stats: {e}")

    balance = account_data.get('balance', 0)
    prompt_balance = account_data.get('available_balance', balance)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0

    logger.debug(f"📈 Extracted from 15m: price={current_price}, atr={atr_15m}")
    logger.debug(f"💳 Prompt balance (available): {prompt_balance} USDT | Snapshot balance (total): {balance} USDT")

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

    formatted_short_memory_text = format_short_memory_text(short_memories)

    next_run_time = calculate_next_run_time(agent_config, now_cn)

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    prompt_template = resolve_prompt_template(agent_config, trade_mode, PROJECT_ROOT, logger)
    leverage = global_config.get_leverage(config_id)
    history_text_for_prompt = formatted_history_text
    if "{short_memory_text" not in prompt_template:
        history_text_for_prompt = f"{formatted_short_memory_text}\n\n{formatted_history_text}"

    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [{"id": o.get('order_id'), "side": o.get('side'), "pos_side": o.get('pos_side'), "price": o.get('price'), "amount": o.get('amount')} for o in raw_orders]
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
        short_memory_text=formatted_short_memory_text,
        history_text=history_text_for_prompt,
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

 

def agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    model_name = agent_config.get('model', '').lower()
    trade_mode = agent_config.get('mode', 'STRATEGY').upper()
    logger.info(f"--- [Node] Agent: {agent_config.get('model')} (Mode: {trade_mode}) ---")

    # Pass history messages as-is; DeepSeekChatOpenAI (used via build_chat_openai for
    # deepseek/r1 models) will inject reasoning_content into the payload automatically.
    messages = list(state.messages)

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
            tools = [open_position_strategy, cancel_orders_strategy, close_position_strategy]
        
        # if trade_mode == 'REAL':
        #     tools += [analyze_event_contract, format_event_contract_order]

        llm = build_chat_openai(
            model=agent_config.get('model'),
            api_key=agent_config.get('api_key'),
            base_url=agent_config.get('api_base'),
            temperature=agent_config.get('temperature', 0.5),
            extra_body=kwargs.get("extra_body"),
        ).bind_tools(tools)

        response = invoke_with_retry(
            lambda: llm.invoke(messages),
            logger=logger,
            context=f"agent symbol={symbol} config_id={config_id} model={agent_config.get('model')}",
        )
        
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

    except LLMInvocationError as e:
        logger.error(f"[LLM Error] ({symbol}) type={e.error_type}: {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}")], "active_agent": "MASTER"})
    except Exception as e:
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

        tools = [open_position_strategy, cancel_orders_strategy, close_position_strategy]
        
        llm = build_chat_openai(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            extra_body=kwargs.get("extra_body"),
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
        "close_position_strategy": close_position_strategy,
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
    
    agent_type = "MASTER"
    final_full_content = f"### 🧠 深度决策分析\n{full_content}" if full_content else ""

    if final_full_content:
        # 汇总逻辑仅针对主要内容
        logic_source = full_content if full_content else final_full_content
        strategy_prompt_template = _resolve_strategy_summary_prompt(agent_config)
        strategy_logic = summarize_content(logic_source, agent_config, prompt_template=strategy_prompt_template)
        
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
        human_message=human_message
    )
    try:
        app.invoke(initial_state, config={"configurable": {"config_id": config_id, "agent_config": config}})
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
