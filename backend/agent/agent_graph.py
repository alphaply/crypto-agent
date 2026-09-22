import uuid
import json
import re
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


import pytz
from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    BaseMessageChunk,
    ToolMessage,
    HumanMessage,
    SystemMessage,
    message_chunk_to_message,
)
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


def _emit_agent_retry(configurable, messages, attempt, total, error_type, exc, model_name: str = ""):
    model_prefix = f"[{model_name}] " if model_name else ""
    _emit_task_progress(
        configurable, phase="thinking",
        message=f"{model_prefix}模型请求失败，正在重试（第 {attempt}/{total} 次请求，{error_type}）",
        retry_attempt=attempt, total_attempts=total, error_type=error_type,
        reasoning_content=_collect_agent_reasoning(messages),
        reasoning_tokens=_collect_agent_reasoning_token_count(messages),
        tool_calls=[],
    )


def _has_complete_tool_arguments(chunk: BaseMessageChunk, response: AIMessage) -> bool:
    """Do not trust LangChain's partial-JSON repair for a length-limited turn."""
    raw_calls = getattr(chunk, "tool_call_chunks", None) or []
    calls = response.tool_calls or []
    if not calls or response.invalid_tool_calls or len(raw_calls) != len(calls):
        return False
    seen_ids = set()
    for raw, call in zip(raw_calls, calls):
        call_id = raw.get("id")
        if not call_id or call_id in seen_ids or call_id != call.get("id"):
            return False
        seen_ids.add(call_id)
        if not raw.get("name") or raw["name"] != call.get("name"):
            return False
        try:
            # json.loads requires closed braces/strings; parse_partial_json does not.
            args = json.loads(raw.get("args"))
        except (TypeError, ValueError):
            return False
        if not isinstance(args, dict) or args != call.get("args"):
            return False
    return True


def _stream_agent_response(
    llm,
    messages: list[BaseMessage],
    *,
    configurable: dict,
    run_config: RunnableConfig | None = None,
) -> AIMessage:
    """Stream one agent turn while preserving reasoning and tool-call chunks."""
    combined_chunk: BaseMessageChunk | None = None
    streamed_reasoning = ""
    last_progress_at = 0.0
    last_progress_size = 0
    trace_config = {
        "metadata": (run_config or {}).get("metadata", {}),
        "tags": (run_config or {}).get("tags", []),
    }

    for chunk in llm.stream(messages, config=trace_config):
        if not isinstance(chunk, BaseMessageChunk):
            continue
        combined_chunk = chunk if combined_chunk is None else combined_chunk + chunk

        reasoning_delta = extract_reasoning_content(chunk)
        if not reasoning_delta:
            continue
        streamed_reasoning += reasoning_delta

        now = time.monotonic()
        should_emit = (
            now - last_progress_at >= 0.5
            or len(streamed_reasoning) - last_progress_size >= 256
        )
        if should_emit:
            partial = AIMessage(
                content="",
                additional_kwargs={"reasoning_content": streamed_reasoning},
            )
            _emit_task_progress(
                configurable,
                phase="thinking",
                message="模型正在流式推理",
                reasoning_content=_collect_agent_reasoning(messages + [partial]),
                reasoning_tokens=_collect_agent_reasoning_token_count(messages),
            )
            last_progress_at = now
            last_progress_size = len(streamed_reasoning)

    if combined_chunk is None:
        raise LLMInvocationError("模型响应流为空，未收到结果。", error_type="empty_response", retryable=True, attempts=1)

    response = message_chunk_to_message(combined_chunk)
    if streamed_reasoning and not extract_reasoning_content(response):
        response.additional_kwargs["reasoning_content"] = streamed_reasoning
    finish_reason = response.response_metadata.get("finish_reason")
    if finish_reason in {"length", "max_tokens"} and _has_complete_tool_arguments(combined_chunk, response):
        # The gateway can finish with length after a complete tool call and a
        # truncated narrative. Keep the call; tools_node still applies normal
        # validation and the following agent turn receives the execution result.
        warning = "模型正文因输出上限截断；工具参数完整，继续工具校验与执行。"
        response.additional_kwargs["output_warning"] = warning
        logger.warning("[LLM] %s finish_reason=%s", warning, finish_reason)
        return response
    if finish_reason in {"length", "max_tokens", "content_filter"} or response.invalid_tool_calls or (
        not extract_message_text(response).strip() and not response.tool_calls
    ):
        detail = "输出额度耗尽" if finish_reason in {"length", "max_tokens"} else "未返回有效正文或工具调用"
        raise LLMInvocationError(
            f"模型响应未完成：{detail}（finish_reason={finish_reason or '未知'}）。",
            error_type="incomplete_response",
            retryable=finish_reason not in {"length", "max_tokens", "content_filter"}, attempts=1,
        )
    return response


def _stream_agent_turn(
    tool_llm,
    messages: list[BaseMessage],
    *,
    configurable: dict,
    run_config: RunnableConfig,
) -> AIMessage:
    """Run exactly one billable model request for a tool-capable agent turn.

    Some compatible gateways expose reasoning token usage without exposing the
    reasoning text. Replaying the prompt without tools to manufacture a visible
    reasoning stream doubles cost and can produce a rationale that does not
    correspond to the tool decision, so missing reasoning stays provider-hidden.
    """
    return _stream_agent_response(
        tool_llm,
        messages,
        configurable=configurable,
        run_config=run_config,
    )


def resolve_market_timeframes(agent_config: dict | None = None) -> list[str]:
    agent_timeframes = [str(item).strip() for item in list((agent_config or {}).get('market_timeframes') or []) if str(item).strip()]
    if agent_timeframes:
        return agent_timeframes

    global_timeframes = [str(item).strip() for item in list(getattr(global_config, 'market_timeframes', None) or []) if str(item).strip()]
    if global_timeframes:
        return global_timeframes

    return ['15m', '1h', '4h', '1d', '1w']
TZ_US = pytz.timezone('America/New_York')
logger = setup_logger("AgentGraph")
load_dotenv()
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def calculate_next_run_time(agent_config, now_cn):
    """计算该 agent 的下次运行时间（用于注入 Prompt）"""
    mode = agent_config.get('mode', 'STRATEGY').upper()

    if mode in ['REAL', 'STRATEGY']:
        from backend.utils.run_schedule import next_scheduled_run
        next_run = next_scheduled_run(agent_config, now_cn)
        return next_run.strftime('%m-%d %H:%M %Z') if next_run else 'N/A'

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
    
    # 获取配置，优先级：1. agent 专属 summarizer -> 2. 全局运行配置/环境变量 -> 3. agent 自身配置
    model = (summarizer_cfg.get("model") or 
             getattr(global_config, "global_summarizer_model", "") or
             os.getenv("GLOBAL_SUMMARIZER_MODEL") or 
             agent_config.get("model"))
    api_key = (summarizer_cfg.get("api_key") or 
               getattr(global_config, "global_summarizer_api_key", "") or
               os.getenv("GLOBAL_SUMMARIZER_API_KEY") or 
               agent_config.get("api_key"))
    api_base = (summarizer_cfg.get("api_base") or 
                getattr(global_config, "global_summarizer_api_base", "") or
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
            "daily": "请依据以下策略和执行证据生成每日复盘，600字以内。包含市场与策略演变、实际成交与结果、计划执行偏差、风险教训和次日条件；计划不等于成交，缺失盈亏/费用标记未知，不重复计数。只输出复盘文本。\n\n内容：\n{content}",
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
        if summary_type in {"daily", "short_memory"}:
            return ""
        return content[:200] + "..."


def is_invalid_daily_summary(summary: str, source_input: str = "") -> bool:
    """Reject empty summaries and prompt/input echoes masquerading as results."""
    text = str(summary or "").strip()
    if not text:
        return True
    prompt_markers = (
        "一整天的多轮交易分析逻辑，请汇总为",
        "请把以下一整天的交易推理压缩成",
        "{content}",
    )
    if any(marker in text for marker in prompt_markers):
        return True
    return bool(source_input) and text.endswith("...") and source_input.startswith(text[:-3])


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
        from backend.utils.trade_review import daily_execution_evidence, daily_exchange_evidence
        execution_evidence = daily_execution_evidence(config_id, date_str)
        exchange_evidence = daily_exchange_evidence(target_config, date_str)
        if exchange_evidence:
            execution_evidence += '\n\n' + exchange_evidence
        if not rows and not execution_evidence:
            logger.info(f"No summary data found for {config_id} on {date_str}")
            return False
            
        combined = "\n".join(
            f"[{r['timestamp']}] {r['strategy_logic']}"
            for r in rows if r.get('strategy_logic')
        )
        if not combined.strip() and not execution_evidence:
            return False
            
        summary_input = (
            f"复盘日期 {date_str}。按【市场与策略变化】【实际执行与结果】【偏差与教训】【下一日条件】总结。"
            "区分计划、挂单、成交、平仓；只用有证据的数字，不把分析轮数当成交数。"
            "检查失效后退出、止损变更、浮亏加仓和成本。缺失的费用/盈亏写未知。\n"
            f"策略记录：\n{combined}\n\n执行记录：\n{execution_evidence or '无本地执行记录，不代表交易所没有交易。'}"
        )
        summary_text = summarize_content(
            summary_input,
            target_config,
            summary_type="daily",
        )
        if is_invalid_daily_summary(summary_text, summary_input):
            logger.error(f"Daily summary returned no usable result for {config_id} on {date_str}; not saving it.")
            return False

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


def parse_execution_facts_to_text(raw_data: str | dict) -> str:
    """Parse structured execution facts JSON into human-readable text for prompt/memory injection."""
    if isinstance(raw_data, str):
        try:
            data = json.loads(raw_data)
        except Exception:
            return raw_data
    elif isinstance(raw_data, dict):
        data = raw_data
    else:
        return ""

    sections = []

    # 1. 已平仓交易周期
    cycles = data.get("position_cycles", {}) or {}
    completed = cycles.get("completed", []) or []
    if completed:
        lines = ["【已平仓交易周期】"]
        for c in completed:
            symbol = c.get("symbol", "")
            side = c.get("side", "")
            pnl = c.get("realized_pnl_before_fees")
            reasons = c.get("exit_reasons", []) or []
            reason_desc = []
            if "stop_loss" in reasons:
                reason_desc.append("止损出场")
            if "take_profit" in reasons:
                reason_desc.append("止盈出场")
            reason_desc.extend(r for r in reasons if r not in {'stop_loss', 'take_profit'})
            reason_str = " | " + " ".join(reason_desc) if reason_desc else ""
            currency = c.get('settlement_currency', 'USDT')
            pnl_str = '盈亏未知' if pnl is None else f"手续费前盈亏 {pnl:+.2f} {currency}"
            entry_vwap = c.get("entry_vwap", 0.0)
            exit_vwap = c.get("exit_vwap", 0.0)
            amount = c.get("entered_base", 0.0)
            lines.append(f"- {symbol} {side} | 入场均价: {entry_vwap:.2f} | 离场均价: {exit_vwap:.2f} | 数量: {amount:.4f} | {pnl_str}{reason_str}")
        sections.append("\n".join(lines))

    # 2. 未闭合持仓周期
    open_cycles = cycles.get("open_cycles", []) or []
    if open_cycles:
        lines = ["【未闭合持仓周期 (实际持仓以实时账户快照为准)】"]
        for oc in open_cycles:
            symbol = oc.get("symbol", "")
            side = oc.get("side", "")
            rem = oc.get("remaining_base", 0.0)
            cost = oc.get("entry_cost", 0.0)
            lines.append(f"- {symbol} {side} | 剩余数量: {rem:.4f} | 累计成本: {cost:.2f} USDT")
        sections.append("\n".join(lines))

    # 3. 近期成交明细
    recent_fills = data.get("recent_fills", []) or []
    if recent_fills:
        lines = ["【近期成交明细】"]
        for f in recent_fills:
            ts = f.get("timestamp") or 0
            t_str = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%m-%d %H:%M:%S") if ts else "未知时间"
            sym = f.get("symbol", "")
            pside = f.get("position_side", "")
            role = f.get("role", "")
            side = f.get("side", "")
            price = f.get("price", 0.0)
            amount = f.get("amount", 0.0)
            lines.append(f"- [{t_str}] {sym} {pside} ({role}/{side}) | 价格: {price} | 数量: {amount}")
        sections.append("\n".join(lines))

    # 4. 账本统计
    lines = ["【账本统计】"]
    pnl = data.get("known_realized_pnl_before_fees")
    if pnl is not None:
        pnl_str = f"+{pnl:.2f}" if pnl > 0 else f"{pnl:.2f}"
        lines.append(f"已确认实现盈亏: {pnl_str} USDT")
    fees = data.get("fees_by_currency", {}) or {}
    if fees:
        for cur, amt in fees.items():
            lines.append(f"手续费: {amt} {cur}")
    sections.append("\n".join(lines))

    return "\n\n".join(sections)


def format_recent_position_history_for_memory(config_id: str, agent_config: dict) -> str:
    """Format recent closed positions and performance summary from local database for short memory summary."""
    try:
        symbol = agent_config.get('symbol', '')
        positions = database.get_closed_positions_7d(
            config_id=config_id,
            symbol=symbol,
            days=7,
            mode=agent_config.get('mode'),
        )
        result = database.format_closed_positions_summary(positions, days=7)
        if str(agent_config.get('mode', '')).upper() == 'REAL':
            from backend.utils.execution_ledger import recent_activity_summary
            try:
                result += '\n' + recent_activity_summary(config_id, symbol)
            except Exception:
                result += '\n最近7天成交活动暂不可用，不能据此断言历史完整。'
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch local closed positions for memory: {e}")
        return ""


def update_turn_memory(config_id: str, agent_config: dict, strategy_logic: str, messages: list) -> bool:
    """Replace bounded working memory after each new strategy, retaining execution evidence."""
    if not str(strategy_logic or "").strip():
        return False
    previous = format_short_memory_for_llm(config_id)
    call_names = {call['id']: call['name'] for msg in messages if isinstance(msg, AIMessage)
                  for call in (msg.tool_calls or [])}
    execution = "\n".join(
        f"{getattr(msg, 'name', None) or call_names.get(msg.tool_call_id, 'tool')}: {extract_message_text(msg)}"
        for msg in messages if isinstance(msg, ToolMessage)
    )
    now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    pos_history_text = format_recent_position_history_for_memory(config_id, agent_config)
    source = (
        f"更新时间：{now}\n旧记忆：\n{previous}\n新策略逻辑：\n{strategy_logic}\n"
        f"本轮工具结果：\n{execution or '无工具执行，不得声称新交易已执行。'}"
    )
    if pos_history_text:
        source += f"\n\n## 近期仓位与成交事实\n{pos_history_text}"
    summary = summarize_content(source, agent_config, summary_type="short_memory")
    if _is_invalid_short_memory_summary(summary, source):
        # Still advance the strategy on summarizer failure; never retain an old plan as current.
        summary = f"【最新策略（压缩失败，待核实执行）】{strategy_logic[:1200]}\n历史计划须重新验证，账户以实时快照为准。"
    if len(summary) > 2400:
        summary = '【压缩输出过长，最新策略待核实】' + strategy_logic[:1200] + '\n账户以实时快照为准。'
    save_short_memory(now, now, agent_config.get("symbol", "Unknown"), config_id, summary, pos_history_text, 1)
    return True


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
    latest = get_short_memories(config_id, limit=1)
    if latest and latest[0].get("bucket_start", "") >= start_text:
        return False  # Per-turn memory already covers this maintenance window.

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
    pos_history_text = format_recent_position_history_for_memory(config_id, target_config)
    memory_input = (
        f"Window: {start_text} - {end_text}\n"
        f"Symbol: {target_config.get('symbol')}\n\n"
        f"Recent market/agent reasoning:\n{market_text or 'No new market reasoning.'}\n\n"
        f"Previous short memory:\n{previous}"
    )
    if pos_history_text:
        memory_input += f"\n\n## 近期仓位与成交事实\n{pos_history_text}"
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

def start_node(state: AgentState, config: RunnableConfig, *, require_fresh: bool = False) -> AgentState:
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
        if require_fresh:
            analysis = market_full.get("analysis", {})
            if any(not analysis.get(tf) or not analysis[tf].get("price")
                   or (analysis[tf].get("data_quality") or {}).get("stale")
                   for tf in timeframes_to_fetch):
                raise ValueError("重试行情刷新不完整，停止本轮决策")
            if account_data.get("error"):
                raise ValueError("重试账户刷新失败，停止本轮决策")
        news_context = fetch_news_risk_context(symbol)
        database.save_news_snapshot(symbol, config_id, news_context)
        daily_history = get_daily_summaries(config_id, days=7)
        short_memory_text = format_short_memory_for_llm(config_id, limit=1)

        logger.debug(f"📊 Market data fetched: {len(market_full.get('analysis', {}))} timeframes")
        logger.debug(f"💰 Account balance: {account_data.get('balance', 0)} USDT")
    except Exception as e:
        if require_fresh:
            raise LLMInvocationError(
                "重试数据刷新失败，已停止本轮决策。",
                error_type="data_refresh_error", retryable=False, attempts=0, original=e,
            ) from e
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
            "data_quality": tf_data.get("data_quality", {}),
            "vwap_anchor": tf_data.get("vwap_anchor"),
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

    formatted_history_text = "## Daily Memory\n" + formatted_history_text

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

    from backend.utils.performance_context import performance_context
    system_prompt += '\n\n' + performance_context(config_id, symbol, trade_mode)

    from backend.utils.trading_policy import trading_policy, protection_context
    system_prompt += '\n\n' + trading_policy(trade_mode)
    if trade_mode == 'REAL':
        try:
            system_prompt += '\n\n' + protection_context(config_id)
        except Exception as exc:
            system_prompt += f'\n保护计划读取失败，不能假设已有保护单：{exc}'
    if '{short_memory_text}' not in prompt_template:
        system_prompt += '\n\n## Short-term memory\n' + short_memory_text
    prompt_role = agent_config.get("system_prompt_role", "system")
    instruction = instruction_message(system_prompt, prompt_role)
    instruction.additional_kwargs["is_instruction"] = True
    if isinstance(instruction, HumanMessage) and state.human_message:
        messages = [HumanMessage(content=f"{system_prompt}\n\n## User request\n{state.human_message}", additional_kwargs={"is_instruction": True})]
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

 

def refresh_decision_context(state: AgentState, config: RunnableConfig) -> AgentState:
    """Replace the snapshot, retaining completed tool calls/results without replaying them."""
    _emit_task_progress(config.get("configurable", {}), phase="thinking",
                        message="正在重新获取行情、计算指标并刷新账户状态")
    try:
        refreshed = start_node(state, config, require_fresh=True)
    except Exception as exc:
        raise LLMInvocationError(
            "重试数据刷新失败，已停止本轮决策。",
            error_type="data_refresh_error", retryable=False, attempts=0, original=exc,
        ) from exc
    # start_node owns the instruction and original user request. Keep subsequent
    # completed conversation turns, including each tool call/result pair.
    history_start = next((i for i, msg in enumerate(state.messages)
                          if isinstance(msg, (AIMessage, ToolMessage))), len(state.messages))
    history = state.messages[history_start:]
    notice = HumanMessage(
        content="行情、指标和账户已刷新，以最新指令中的快照为准。之前的工具结果是执行历史，不要重复执行已完成的交易；重新评估价格和止盈止损。",
        additional_kwargs={"decision_refresh_notice": True},
    )
    history = [msg for msg in history if not msg.additional_kwargs.get("decision_refresh_notice")]
    return refreshed.model_copy(update={"messages": refreshed.messages + history + [notice]})


def adapt_messages_for_prompt_role(messages: list[BaseMessage], system_prompt_role: str | None) -> list[BaseMessage]:
    """Adapt instruction message class (SystemMessage vs HumanMessage) if target provider requires user role."""
    target_role = str(system_prompt_role or "system").strip().lower()
    if not messages:
        return messages
    first = messages[0]
    if target_role == "user" and isinstance(first, SystemMessage):
        kwargs = dict(first.additional_kwargs)
        kwargs["is_instruction"] = True
        return [HumanMessage(content=first.content, additional_kwargs=kwargs)] + list(messages[1:])
    elif target_role == "system" and isinstance(first, HumanMessage) and first.additional_kwargs.get("is_instruction"):
        kwargs = dict(first.additional_kwargs)
        return [SystemMessage(content=first.content, additional_kwargs=kwargs)] + list(messages[1:])
    return messages


def resolve_decision_model_chain(agent_config: dict) -> list[dict]:
    """
    Resolve the ordered list of decision model configurations for an agent:
    [primary_model, fallback_1, fallback_2, ...]
    """
    chain: list[dict] = []

    primary_model = agent_config.get("model")
    if primary_model:
        chain.append({
            "model": primary_model,
            "api_key": agent_config.get("api_key"),
            "api_base": agent_config.get("api_base") or "",
            "temperature": agent_config.get("temperature", 0.5),
            "extra_body": agent_config.get("extra_body") or {},
            "thinking_enabled": agent_config.get("thinking_enabled"),
            "reasoning_effort": agent_config.get("reasoning_effort") or "",
            "compatibility_mode": agent_config.get("compatibility_mode") or "auto",
            "system_prompt_role": agent_config.get("system_prompt_role", "system"),
            "provider_id": agent_config.get("llm_provider_id"),
            "name": agent_config.get("model_name") or primary_model,
        })

    fallback_models = agent_config.get("fallback_models") or []
    for item in fallback_models:
        if isinstance(item, dict) and item.get("model"):
            chain.append({
                "model": item.get("model"),
                "api_key": item.get("api_key") or agent_config.get("api_key"),
                "api_base": item.get("api_base") or "",
                "temperature": item.get("temperature", agent_config.get("temperature", 0.5)),
                "extra_body": item.get("extra_body") or {},
                "thinking_enabled": item.get("thinking_enabled"),
                "reasoning_effort": item.get("reasoning_effort") or "",
                "compatibility_mode": item.get("compatibility_mode") or "auto",
                "system_prompt_role": item.get("system_prompt_role", "system"),
                "provider_id": item.get("provider_id"),
                "name": item.get("name") or item.get("model"),
            })

    if len(chain) <= 1 and agent_config.get("fallback_llm_provider_ids"):
        try:
            from backend.config_store import load_runtime_snapshot
            snapshot = load_runtime_snapshot()
            if snapshot:
                provider_map = {p["provider_id"]: p for p in snapshot.get("llm_providers", [])}
                for pid in agent_config.get("fallback_llm_provider_ids", []):
                    prov = provider_map.get(str(pid).strip())
                    if prov and prov.get("model"):
                        chain.append({
                            "model": prov.get("model"),
                            "api_key": prov.get("api_key"),
                            "api_base": prov.get("api_base") or "",
                            "temperature": prov.get("temperature", 0.5),
                            "extra_body": prov.get("extra_body") or {},
                            "thinking_enabled": prov.get("thinking_enabled"),
                            "reasoning_effort": prov.get("reasoning_effort") or "",
                            "compatibility_mode": prov.get("compatibility_mode") or "auto",
                            "system_prompt_role": prov.get("system_prompt_role", "system"),
                            "provider_id": prov.get("provider_id"),
                            "name": prov.get("name") or prov.get("model"),
                        })
        except Exception as resolve_err:
            logger.warning(f"Failed to resolve fallback_llm_provider_ids: {resolve_err}")

    return chain


def agent_node(state: AgentState, config: RunnableConfig) -> AgentState:
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})

    symbol = state.symbol
    trade_mode = agent_config.get("mode", "STRATEGY").upper()

    chain = resolve_decision_model_chain(agent_config)
    if not chain:
        chain = [{
            "model": agent_config.get("model", "default"),
            "api_key": agent_config.get("api_key"),
            "api_base": agent_config.get("api_base") or "",
            "temperature": agent_config.get("temperature", 0.5),
            "extra_body": agent_config.get("extra_body") or {},
            "thinking_enabled": agent_config.get("thinking_enabled"),
            "reasoning_effort": agent_config.get("reasoning_effort"),
            "compatibility_mode": agent_config.get("compatibility_mode"),
            "system_prompt_role": agent_config.get("system_prompt_role", "system"),
            "name": agent_config.get("model", "default"),
        }]

    tools = get_trade_tools_for_mode(trade_mode)
    logger.info(
        "[ToolRegistry] Bound tools for mode=%s: %s",
        trade_mode,
        [tool.name for tool in tools],
    )

    total_models = len(chain)
    start_idx = state.active_model_idx if (state.active_model_idx is not None and 0 <= state.active_model_idx < total_models) else 0

    attempted_errors: list[tuple[str, str]] = []
    request_attempts = 0

    for chain_idx in range(start_idx, total_models):
        current_cfg = chain[chain_idx]
        current_model = current_cfg.get("model") or "unknown"
        current_name = current_cfg.get("name") or current_model
        is_fallback = (chain_idx > 0)

        logger.info(
            f"--- [Node] Agent turn: {current_name} ({current_model}) "
            f"[Chain {chain_idx + 1}/{total_models}, Mode: {trade_mode}] ---"
        )

        turn_messages = adapt_messages_for_prompt_role(state.messages, current_cfg.get("system_prompt_role"))

        _emit_task_progress(
            configurable,
            phase="thinking",
            message=(
                f"使用兜底模型 [{current_name}] 分析市场并规划工具调用（链路第 {chain_idx + 1}/{total_models} 个模型）"
                if is_fallback
                else "模型正在分析市场并规划工具调用"
            ),
            reasoning_content=_collect_agent_reasoning(turn_messages),
            reasoning_tokens=_collect_agent_reasoning_token_count(turn_messages),
        )

        try:
            kwargs = {}
            if current_cfg.get("extra_body"):
                kwargs["extra_body"] = current_cfg.get("extra_body")

            reasoning_llm = build_chat_model(
                model=current_cfg.get("model"),
                api_key=current_cfg.get("api_key"),
                base_url=current_cfg.get("api_base"),
                temperature=current_cfg.get("temperature", 0.5),
                extra_body=kwargs.get("extra_body"),
                thinking_enabled=current_cfg.get("thinking_enabled"),
                reasoning_effort=current_cfg.get("reasoning_effort"),
                compatibility_mode=current_cfg.get("compatibility_mode"),
                streaming=True,
            )
            llm = reasoning_llm.bind_tools(tools)

            def invoke_decision():
                nonlocal state, turn_messages, request_attempts
                if request_attempts:
                    state = refresh_decision_context(state, config)
                request_attempts += 1
                turn_messages = adapt_messages_for_prompt_role(state.messages, current_cfg.get("system_prompt_role"))
                return _stream_agent_turn(llm, turn_messages, configurable=configurable, run_config=config)

            response = invoke_with_retry(
                invoke_decision,
                logger=logger,
                context=f"agent symbol={symbol} config_id={config_id} model={current_model} chain={chain_idx + 1}/{total_models}",
                on_retry=lambda attempt, total, error_type, exc: _emit_agent_retry(
                    configurable, turn_messages, attempt, total, error_type, exc, model_name=current_name,
                ),
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
                message=response.additional_kwargs.get("output_warning") or ("模型已生成工具调用计划" if response_tool_calls else "模型分析完成，正在整理结果"),
                reasoning_content=_collect_agent_reasoning(turn_messages + [response]),
                reasoning_tokens=_collect_agent_reasoning_token_count(turn_messages + [response]),
                tool_calls=response_tool_calls,
            )

            try:
                usage = response.response_metadata.get("token_usage", {})
                if usage:
                    database.save_token_usage(
                        symbol=symbol,
                        config_id=config_id,
                        model=current_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
            except Exception as usage_e:
                logger.warning(f"⚠️ [Agent] Failed to save token usage: {usage_e}")

            return state.model_copy(update={
                "messages": turn_messages + [response],
                "active_agent": "MASTER",
                "active_model_idx": chain_idx,
                "active_model_name": current_model,
            })

        except Exception as e:
            err_desc = str(e)
            attempted_errors.append((current_name, err_desc))
            has_next = (chain_idx + 1 < total_models)
            if isinstance(e, LLMInvocationError) and e.error_type == "data_refresh_error":
                break

            if has_next:
                next_model_cfg = chain[chain_idx + 1]
                next_name = next_model_cfg.get("name") or next_model_cfg.get("model")
                logger.warning(
                    f"⚠️ [Fallback Triggered] Model [{current_name}] failed: {e}. "
                    f"Switching to fallback model [{next_name}] ({chain_idx + 2}/{total_models}) for {symbol}."
                )
                _emit_task_progress(
                    configurable,
                    phase="thinking",
                    message=f"决策模型 [{current_name}] 请求失败，正在切换至兜底模型 [{next_name}]（第 {chain_idx + 2}/{total_models} 个模型）...",
                    reasoning_content=_collect_agent_reasoning(turn_messages),
                    reasoning_tokens=_collect_agent_reasoning_token_count(turn_messages),
                )
                continue
            else:
                logger.error(
                    f"❌ [All Models Failed] All {total_models} models in fallback chain failed for {symbol}: {attempted_errors}"
                )

    from backend.utils.llm_utils import get_llm_max_retries
    retries_per_model = max(get_llm_max_retries(), 0)
    attempts_per_model = retries_per_model + 1
    err_summary = "; ".join([f"[{m}]: {err}" for m, err in attempted_errors])
    final_error_msg = f"决策模型链路失败或中止（已尝试 {len(attempted_errors)} 个模型，每模型最多 {attempts_per_model} 次）：{err_summary}"
    _emit_task_progress(configurable, phase="failed", message=final_error_msg)
    logger.error(f"[LLM Error] ({symbol}): {final_error_msg}")
    return state.model_copy(update={
        "messages": state.messages + [AIMessage(content=f"Error: {final_error_msg}", additional_kwargs={"invocation_failed": True})],
        "active_agent": "MASTER",
    })

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
        
        reasoning_llm = build_chat_model(
            model=model_name,
            api_key=api_key,
            base_url=api_base,
            temperature=temperature,
            extra_body=kwargs.get("extra_body"),
            thinking_enabled=agent_config.get("thinking_enabled"),
            reasoning_effort=agent_config.get("reasoning_effort"),
            compatibility_mode=agent_config.get("compatibility_mode"),
            streaming=True,
        )
        llm = reasoning_llm.bind_tools(tools)

        request_attempts = 0

        def invoke_decision():
            nonlocal state, messages, request_attempts
            if request_attempts:
                state = refresh_decision_context(state, config)
            request_attempts += 1
            messages = state.messages
            return _stream_agent_turn(llm, messages, configurable=configurable, run_config=config)

        response = invoke_with_retry(
            invoke_decision,
            logger=logger,
            context=f"small-agent symbol={symbol} config_id={config_id} model={model_name}",
            on_retry=lambda attempt, total, error_type, exc: _emit_agent_retry(
                configurable, messages, attempt, total, error_type, exc,
            ),
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
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}", additional_kwargs={"invocation_failed": True})], "active_agent": "MASTER"})
    except Exception as e:
        logger.error(f"❌ [Small Agent Error] ({symbol}): {e}")
        return state.model_copy(update={"messages": state.messages + [AIMessage(content=f"Error: {str(e)}", additional_kwargs={"invocation_failed": True})], "active_agent": "MASTER"})

def _collect_agent_reasoning(messages: list[BaseMessage]) -> str:
    sections: list[tuple[str, list[str]]] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        reasoning = extract_reasoning_content(message).strip()
        reasoning_tokens = extract_reasoning_token_count(message)
        if not reasoning and not reasoning_tokens:
            continue
        if not reasoning:
            reasoning = (
                f"模型使用了 {reasoning_tokens} 个推理 token，"
                "但上游接口没有返回可展示的思考摘要。"
            )
        tool_names = [str(call.get("name") or "") for call in (message.tool_calls or []) if call.get("name")]
        sections.append((reasoning, tool_names))

    # A single provider response is one coherent reasoning stream. Adding a
    # synthetic "stage 1" heading creates noise and can be mistaken for model
    # output, so stage labels are reserved for real multi-turn/tool runs.
    if len(sections) == 1:
        return sections[0][0]

    formatted: list[str] = []
    for index, (reasoning, tool_names) in enumerate(sections, start=1):
        label = f"### 推理阶段 {index}"
        if tool_names:
            label += f" · 调用 {', '.join(tool_names)}"
        formatted.append(f"{label}\n\n{reasoning}")
    return "\n\n---\n\n".join(formatted)


def _collect_agent_reasoning_token_count(messages: list[BaseMessage]) -> int:
    return sum(
        extract_reasoning_token_count(message)
        for message in messages
        if isinstance(message, AIMessage)
    )


def finalize_node(state: AgentState, config: RunnableConfig) -> AgentState:
    """合并 AI 消息的内容并保存到数据库。"""
    configurable = config.get("configurable", {})
    config_id = configurable.get("config_id", "unknown")
    agent_config = configurable.get("agent_config", {})
    
    symbol = state.symbol
    agent_name = state.active_model_name or agent_config.get('model', 'Unknown')
    
    all_ai_messages = [
        (msg, (f"[输出提示] {msg.additional_kwargs['output_warning']}\n\n" if msg.additional_kwargs.get("output_warning") else "") + extract_message_text(msg))
        for msg in state.messages
        if isinstance(msg, AIMessage) and (extract_message_text(msg) or msg.additional_kwargs.get("output_warning"))
    ]
    
    full_content = ""
    if all_ai_messages:
        full_content = "\n\n---\n\n".join(text for _, text in all_ai_messages)
    
    agent_type = "MASTER"
    final_full_content = full_content
    reasoning_content = _collect_agent_reasoning(state.messages)
    reasoning_tokens = _collect_agent_reasoning_token_count(state.messages)

    failed = any(isinstance(msg, AIMessage) and msg.additional_kwargs.get("invocation_failed") for msg in state.messages)
    if not final_full_content:
        failed = True
        final_full_content = "Error: 模型未返回有效结果，本次分析未完成。"
    if final_full_content:
        # 汇总逻辑仅针对主要内容
        logic_source = full_content if full_content else final_full_content
        strategy_logic = final_full_content if failed else summarize_content(logic_source, agent_config)
        
        try:
            database.save_summary(
                symbol,
                agent_name,
                final_full_content,
                strategy_logic,
                config_id=config_id,
                agent_type=agent_type,
                reasoning_content=reasoning_content,
                reasoning_tokens=reasoning_tokens,
            )
            if not failed:
                update_turn_memory(config_id, agent_config, strategy_logic, state.messages)
            
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
            _emit_task_progress(configurable, phase="failed", message=f"结果保存失败：{e}")
            raise

    _emit_task_progress(
        configurable,
        phase="failed" if failed else "completed",
        message="分析失败，错误记录已保存" if failed else "分析与工具执行结果已保存",
        reasoning_content=reasoning_content,
        reasoning_tokens=reasoning_tokens,
    )

    if failed:
        raise RuntimeError(final_full_content)
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
            reasoning_tokens=_collect_agent_reasoning_token_count(state.messages),
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
        reasoning_tokens=_collect_agent_reasoning_token_count(state.messages),
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
        raise
