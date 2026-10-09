import uuid
import json
import os
import time
from datetime import datetime
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
from backend.agent.tool_registry import get_trade_tools_for_mode, run_trade_tool, tool_result_status
from backend.agent.decision_context import load_decision_memory, render_decision_prompt, clean_decision_template
from backend.agent.call_audit import audited_invoke
from backend.utils.decision_journal import decision_journal
from backend.utils.exit_policy import effective_exit_mode
from backend.utils.spot_portfolio import get_config_symbols, SPOT_MARKET_TIMEFRAMES
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
    require_complete_response,
    resolve_summarizer_provider_id,
    sync_langsmith_environment,
)
from backend.utils.logger import setup_logger
from backend.utils.prompt_utils import resolve_prompt_file_content, resolve_prompt_template, render_prompt
from backend.utils.trading_policy import SUMMARY_FACT_POLICY

import backend.database as database
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

    if str((agent_config or {}).get('mode') or '').upper() == 'SPOT_DCA':
        return list(SPOT_MARKET_TIMEFRAMES)
    if (agent_config or {}).get('market_profile') == 'hourly':
        return ['1h', '4h', '1d']

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
        from backend.utils.run_schedule import dca_slots
        slots = dca_slots(agent_config, now_cn, future=True)
        return slots[0].strftime('%m-%d %H:%M %Z') if slots else 'N/A'
    return "N/A"

# ==========================================
# 1. Summarizer Pipeline
# ==========================================

STRATEGY_SUMMARY_FAILURE_PREFIX = "【策略压缩失败；以下为未压缩原始记录】\n"


def summarize_content(content: str, agent_config: dict, summary_type: str = "strategy") -> str:
    """使用独立的 LLM 配置对分析内容进行压缩。"""
    if summary_type != 'strategy':
        raise ValueError(f'Unsupported summary type: {summary_type}')
    summarizer_cfg = agent_config.get("summarizer") or {}
    
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
            extra_body=summarizer_cfg.get("extra_body"),
            thinking_enabled=summarizer_cfg.get("thinking_enabled"),
            reasoning_effort=summarizer_cfg.get("reasoning_effort"),
            compatibility_mode=summarizer_cfg.get("compatibility_mode"),
        )
        default_prompt = "请把以下单轮交易分析压缩为精炼的中文策略摘要。合并重复分析，保留趋势判断、关键价位、风险点、持仓/挂单意图、实际执行结果和下一步条件。不要为字数目标截断条件或结果。只输出摘要文本。\n\n内容：\n{content}"
        prompt_template = str(agent_config.get('strategy_prompt') or summarizer_cfg.get('strategy_prompt') or "").strip()
        if not prompt_template:
            prompt_template = resolve_prompt_file_content(
                agent_config.get('strategy_prompt_file') or summarizer_cfg.get('strategy_prompt_file'),
                PROJECT_ROOT,
                logger,
                fallback=default_prompt,
            )
        prompt = render_prompt(prompt_template, content=content)
        if '{content}' not in prompt_template:
            prompt += '\n\n内容：\n' + content
        from backend.agent.summary_prompts import STRATEGY_BREVITY_POLICY
        prompt += '\n\n' + SUMMARY_FACT_POLICY + '\n\n' + STRATEGY_BREVITY_POLICY
        prompt_role = summarizer_cfg.get('system_prompt_role') or agent_config.get('system_prompt_role', 'system')
        summary_messages = [instruction_message(prompt, prompt_role)]
        def validate_summary_response(response) -> None:
            # Rejected output still consumed tokens. Record usage before checking
            # completeness, and let the audit retain the actual rejected answer.
            try:
                usage = (getattr(response, 'usage_metadata', None)
                         or response.response_metadata.get('token_usage', {}))
                if usage:
                    database.save_token_usage(
                        symbol=agent_config.get('symbol', 'System'),
                        config_id=agent_config.get('config_id', 'summarizer'),
                        model=model,
                        prompt_tokens=usage.get('input_tokens', usage.get('prompt_tokens', 0)),
                        completion_tokens=usage.get('output_tokens', usage.get('completion_tokens', 0)),
                    )
            except Exception as usage_e:
                logger.warning(f'⚠️ [Summarizer] Failed to save token usage: {usage_e}')
            require_complete_response(response, context=f'{summary_type} summary')
            if not extract_message_text(response).strip():
                raise ValueError('Summarizer returned empty content')

        response = invoke_with_retry(
            lambda: audited_invoke(
                lambda: llm.invoke(summary_messages),
                config_id=agent_config.get('config_id'),
                purpose='strategy_summary',
                model=model, messages=summary_messages,
                provider_id=resolve_summarizer_provider_id(agent_config),
                request_settings=summarizer_cfg,
                response_validator=validate_summary_response),
            logger=logger,
            context=f"summarizer model={model} config_id={agent_config.get('config_id', 'summarizer')}",
        )
        return extract_message_text(response).strip()
    except Exception as e:
        logger.error(f"❌ [Summarizer Error]: {e}")
        # Preserve the source when organization fails; slicing it silently loses
        # the strategy's risk conditions and later execution facts.
        return STRATEGY_SUMMARY_FAILURE_PREFIX + content


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
    spot_config_fingerprint = None
    if trade_mode == 'SPOT_DCA':
        from backend.utils.spot_config_guard import spot_config_lock, spot_execution_fingerprint
        with spot_config_lock:
            spot_config_fingerprint = spot_execution_fingerprint(agent_config)
    is_real_exec = (trade_mode in ['REAL', 'SPOT_DCA'])
    agent_name = config_id
    symbols = get_config_symbols(agent_config) or [symbol]
    multi_spot = trade_mode == 'SPOT_DCA' and len(symbols) > 1
    if trade_mode == 'SPOT_DCA':
        symbol = symbols[0]

    # 提取定投周期与预算逻辑 (SPOT_DCA 专属)
    dca_period_text = "每天"
    dca_budget = agent_config.get('dca_amount')
    if dca_budget is None:
        dca_budget = agent_config.get('dca_budget') if agent_config.get('dca_budget') is not None else 100
    
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
        markets, accounts, news_by_symbol = {}, {}, {}
        for asset in symbols:
            market = market_tool.get_market_analysis(asset, mode=trade_mode, timeframes=timeframes_to_fetch)
            account = market_tool.get_account_status(asset, is_real=is_real_exec, agent_name=agent_name, config_id=config_id)
            if require_fresh or trade_mode == 'SPOT_DCA':
                analysis = market.get("analysis", {})
                if any(not analysis.get(tf) or not analysis[tf].get("price")
                       or (analysis[tf].get("data_quality") or {}).get("stale")
                       for tf in timeframes_to_fetch):
                    raise ValueError(f"{asset} 行情刷新不完整，停止本轮决策")
                if account.get("error"):
                    raise ValueError(f"{asset} 账户刷新失败，停止本轮决策")
            news = fetch_news_risk_context(asset)
            database.save_news_snapshot(asset, config_id, news)
            markets[asset], accounts[asset], news_by_symbol[asset] = market, account, news
        if trade_mode == 'SPOT_DCA':
            from backend.utils.spot_context import scope_spot_accounts
            accounts = scope_spot_accounts(config_id, accounts)
        market_full, account_data, news_context = markets[symbol], accounts[symbol], news_by_symbol[symbol]
        if multi_spot:
            market_full = {**market_full, "symbols": symbols, "by_symbol": markets}
            account_data = {
                **account_data,
                "by_symbol": accounts,
                # Every asset uses the same quote wallet. Never sum the same cash.
                "balance": min(float(a.get('balance') or 0) for a in accounts.values()),
                "available_balance": min(float(a.get('available_balance') or 0) for a in accounts.values()),
                "real_positions": [p for a in accounts.values() for p in a.get('real_positions', [])],
                "real_open_orders": [{**o, 'symbol': o.get('symbol') or asset}
                                     for asset, a in accounts.items() for o in a.get('real_open_orders', [])],
                "external_open_orders": [o for a in accounts.values() for o in a.get('external_open_orders', [])],
            }

        logger.debug(f"📊 Market data fetched: {len(market_full.get('analysis', {}))} timeframes")
        logger.debug(f"💰 Account balance: {account_data.get('balance', 0)} USDT")
    except Exception as e:
        if require_fresh or trade_mode == 'SPOT_DCA':
            raise LLMInvocationError(
                f"行情或账户刷新失败，已停止本轮决策：{e}",
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

    memory_context = load_decision_memory(config_id)

    if is_real_exec:
        try:
            balance = account_data.get('balance', 0)
            positions = account_data.get('real_positions', [])
            total_unrealized_pnl = sum(float(p.get('unrealized_pnl') or 0) for p in positions)
            
            if now.minute < 15 and trade_mode != 'SPOT_DCA':
                database.save_balance_snapshot(symbol, balance, total_unrealized_pnl, config_id=config_id)
                
            for asset in symbols:
                recent_trades = market_tool.fetch_recent_trades(asset, limit=10)
                if recent_trades:
                    if trade_mode == 'SPOT_DCA':
                        with database.get_db_conn() as conn:
                            owned_ids = {str(row[0]) for row in conn.execute(
                                "SELECT order_id FROM orders WHERE config_id=? AND symbol=? AND trade_mode='SPOT_DCA' "
                                "AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'", (config_id, asset))}
                        recent_trades = [t for t in recent_trades if str(t.get('order')) in owned_ids]
                        database.save_trade_history(recent_trades, config_id=config_id)
                    else:
                        database.save_trade_history(recent_trades)
                        database.sync_trade_position_history(config_id, asset, recent_trades)
                asset_positions = accounts[asset].get('real_positions', []) if multi_spot else positions
                if trade_mode != 'SPOT_DCA':
                    database.sync_open_position_history(config_id, asset, asset_positions)
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
            "indicator_profile": tf_data.get("indicator_profile"),
            "spot_context": tf_data.get("spot_context", {}),
            "vwap_anchor": tf_data.get("vwap_anchor"),
            "price": tf_data.get("price"),
            "trend": tf_data.get("trend", {}),
            "recent_opens": tf_data.get("recent_opens", []),
            "recent_closes": tf_data.get("recent_closes", []),
            "recent_highs": tf_data.get("recent_highs", []),
            "recent_lows": tf_data.get("recent_lows", []),
            "recent_times": tf_data.get("recent_times", []),
            "recent_volumes": tf_data.get("recent_volumes", []),
            "decision_context": tf_data.get("decision_context", {}),
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
    if multi_spot:
        blocks = []
        for asset, market in markets.items():
            analysis = market.get('analysis', {})
            primary = next(iter(analysis.values()), {})
            blocks.append(f"## {asset}\n" + format_market_data_to_text({
                'current_price': primary.get('price'), 'atr_base': primary.get('atr'),
                'sentiment': market.get('sentiment'), 'news_context': news_by_symbol[asset],
                'technical_indicators': analysis,
            }))
        formatted_market_data = '\n\n'.join(blocks)
    prompt_template = clean_decision_template(resolve_prompt_template(agent_config, trade_mode, PROJECT_ROOT, logger))
    next_run_time = calculate_next_run_time(agent_config, now_cn)

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    leverage = global_config.get_leverage(config_id)

    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [
            {
                **o,
                "id": o.get('order_id') or o.get('id'),
                "symbol": o.get('symbol') or symbol,
                "side": o.get('side'),
                "pos_side": o.get('pos_side'),
                "type": o.get('type'),
                "price": o.get('price'),
                "amount": o.get('amount'),
            }
            for o in raw_orders
        ]
        protection_plans = None
        if trade_mode == 'REAL':
            from backend.utils.order_context import load_protection_plans

            try:
                protection_plans = load_protection_plans(config_id)
            except Exception as exc:
                logger.warning(f"Protection plan read failed: {exc}")
                protection_plans = [{'symbol': symbol, 'side': 'UNKNOWN',
                                     'read_error': '保护计划读取失败，不能假设已有保护'}]
        orders_friendly_text = format_orders_to_agent_friendly(
            display_orders, None if effective_exit_mode(agent_config) == 'independent_exits' else protection_plans, symbol)
    else:
        raw_mock_orders = account_data.get('mock_open_orders', [])
        
        # 区分未成交的挂单和已入场的模拟持仓
        active_mock_orders = [o for o in raw_mock_orders if not int(o.get('is_filled', 0))]
        active_mock_positions = [o for o in raw_mock_orders if int(o.get('is_filled', 0))]
        
        display_mock_orders = [{"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'),
                                "amount": o.get('amount'), "tp": o.get('take_profit'), "sl": o.get('stop_loss')}
                               for o in active_mock_orders]
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

    if trade_mode in {'REAL', 'STRATEGY'} and effective_exit_mode(agent_config) == 'independent_exits':
        from backend.utils.exit_context import independent_exit_context
        contract_size = None
        if trade_mode == 'REAL':
            try:
                contract_size = float(market_tool.exchange.market(symbol).get('contractSize') or 1)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        orders_friendly_text += '\n\n' + independent_exit_context(
            account_data, plans=protection_plans if trade_mode == 'REAL' else None,
            contract_size=contract_size, symbol=symbol)

    system_prompt = render_decision_prompt(
        prompt_template, memory_context,
        model=agent_config.get('model'),
        symbol=', '.join(symbols),
        leverage=leverage,
        current_time=current_time_str,
        next_run_time=next_run_time,
        current_price=current_price,
        atr_15m=atr_15m,
        balance=prompt_balance,
        positions_text=positions_text,
        orders_text=orders_friendly_text,
        formatted_market_data=formatted_market_data,
        dca_period_text=dca_period_text,
        dca_budget=dca_budget
    )

    # Historical performance/closed-position detail is compressed by memory updates;
    # tool interfaces and execution semantics are carried by native tool descriptions.
    if trade_mode == 'SPOT_DCA':
        from backend.utils.spot_execution import spot_budget_status
        budget_status = spot_budget_status(config_id, agent_config, configurable.get('spot_cycle_id'))
        quote = symbol.split('/')[-1]
        system_prompt += (
            f'\n\n## 现货组合执行约束\n允许标的：{", ".join(symbols)}。计价币：{quote}。'
            f'每次运行预算 {dca_budget} {quote} 由所有标的共享，由你分配；本轮重试和多次工具调用不重置额度。'
            f'任务累计预算：{agent_config.get("dca_budget") if agent_config.get("dca_budget") is not None else "未设置"} {quote}。'
        )
        if '{positions_text}' not in prompt_template:
            system_prompt += '\n\n## 当前持仓\n' + positions_text
        system_prompt += (
            f'\n预算周期：{budget_status["period_id"]}（上海时间）；'
            f'本周期已成交及挂单占用 {budget_status["period_spent"]:g} {quote}，'
            f'本周期剩余 {budget_status["period_remaining"]:g} {quote}；'
            f'任务累计已占用 {budget_status["lifetime_committed"]:g} {quote}。'
            f'预算允许新增买入上限 {budget_status["available"]:g} {quote}，'
            '实际下单还须满足账户可用余额；最终以执行时校验为准。'
        )
        if budget_status['pending_unknown']:
            system_prompt += '存在尚未核实结果的下单，当前禁止新增买入，必须先核实成交/挂单结果。'
        external_count = len(account_data.get('external_open_orders') or [])
        system_prompt += (
            f'\n当前挂单列表仅包含本任务订单；所选标的另有 {external_count} 笔其他/未归属挂单，'
            '其资金占用已体现在可用余额内，本任务无权撤销。'
        )
        if '{orders_text}' not in prompt_template:
            system_prompt += '\n\n## 当前挂单\n' + orders_friendly_text
    if trade_mode in {'REAL', 'STRATEGY'} and '{orders_text}' not in prompt_template:
        system_prompt += '\n\n## 当前挂单\n' + orders_friendly_text
    if agent_config.get('market_profile') == 'hourly':
        system_prompt += '\n\n## 当前小时级策略周期\n每1h评估一次；1h决定触发和失效，4h判断结构，1d提供背景。仅使用本轮提供的已收盘K线，不要求15m入场信号。给出单一主策略和未来1h/4h的条件判断。'
    if trade_mode in {'REAL', 'STRATEGY'}:
        exit_mode = effective_exit_mode(agent_config)
        system_prompt += f'\n\n当前退出模式：exit_mode={exit_mode}。'
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
        "symbol": symbol,
        "market_context": market_full,
        "account_context": account_data,
        "spot_config_fingerprint": spot_config_fingerprint,
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
                return audited_invoke(
                    lambda: _stream_agent_turn(llm, turn_messages, configurable=configurable, run_config=config),
                    config_id=config_id, purpose='decision', model=current_model,
                    provider_id=current_cfg.get('provider_id'),
                    messages=turn_messages, tools=tools)

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
            return audited_invoke(
                lambda: _stream_agent_turn(llm, messages, configurable=configurable, run_config=config),
                config_id=config_id, purpose='decision', model=model_name, messages=messages, tools=tools,
                provider_id=agent_config.get('llm_provider_id'))

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
    if not final_full_content.strip():
        failed = True
        final_full_content = "Error: 模型未返回有效结果，本次分析未完成。"
    if final_full_content:
        # 汇总逻辑仅针对主要内容
        logic_source = final_full_content
        journal = decision_journal(state.messages, fallback=logic_source)
        if not failed:
            _emit_task_progress(configurable, phase="summarizing", message="决策与工具调用已结束，正在生成策略摘要")
        strategy_logic = journal if failed else summarize_content(
            journal,
            {**agent_config, 'config_id': config_id, 'symbol': symbol},
            summary_type='strategy',
        )
        summary_status = ('skipped' if failed else 'failed'
                          if strategy_logic.startswith(STRATEGY_SUMMARY_FAILURE_PREFIX) else 'completed')
        from backend.utils.decision_record import build_decision_record
        decision_record = build_decision_record(state.messages, summary_status)
        run_id = configurable.get('run_id') or 'manual:' + uuid.uuid4().hex
        
        try:
            summary_id = database.save_summary(
                symbol,
                agent_name,
                final_full_content,
                strategy_logic,
                config_id=config_id,
                agent_type=agent_type,
                reasoning_content=reasoning_content,
                reasoning_tokens=reasoning_tokens,
                decision_json=json.dumps(decision_record, ensure_ascii=False),
                enqueue_memory=summary_status == "completed",
                run_id=run_id,
                timeframe='1h' if agent_config.get('market_profile') == 'hourly' else (resolve_market_timeframes(agent_config) or ['1h'])[0],
            )
            
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
    stopped = False
    for tool_call in tool_calls:
        tool_name = tool_call["name"]
        args = tool_call.get("args", {})
        if stopped:
            tool_outputs.append(ToolMessage(tool_call_id=tool_call['id'], content=json.dumps({
                'status': 'not_executed', 'reason': '前序工具失败或结果待核验，本轮后续操作已停止。'
            }, ensure_ascii=False)))
            for item in progress_calls:
                if item['id'] == str(tool_call.get('id') or ''):
                    item['status'] = 'not_executed'
            continue
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
            extra = {}
            if str(configurable.get('agent_config', {}).get('mode') or '').upper() == 'SPOT_DCA':
                extra['cycle_id'] = configurable.get('spot_cycle_id')
                extra['expected_spot_fingerprint'] = state.spot_config_fingerprint or ''
            result = run_trade_tool(tool_name, args, config_id, symbol, operation_id=tool_call['id'], **extra)
            tool_outputs.append(ToolMessage(tool_call_id=tool_call["id"], content=result))
            status = tool_result_status(result)
        except Exception as e:
            logger.error("Error executing tool %s: %s", tool_name, e)
            tool_outputs.append(ToolMessage(tool_call_id=tool_call["id"], content=f"Error: {str(e)}"))
            status = "failed"
        stopped = status in {'failed', 'unknown', 'pending'}
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

def run_agent_for_config(config: dict, human_message: str = None, progress_callback=None, *, run_id: str | None = None):
    config_id = config.get('config_id', 'unknown')
    symbol = config['symbol']
    run_id = run_id or 'manual:' + uuid.uuid4().hex
    with database.get_db_conn() as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(summaries)')}
        if 'run_id' in columns and conn.execute('SELECT 1 FROM summaries WHERE config_id=? AND run_id=?',
                                               (config_id, run_id)).fetchone():
            logger.info('Run %s already has a persisted result; trading is not replayed', run_id)
            return
    if str(config.get('mode') or '').upper() == 'SPOT_DCA':
        from backend.utils.spot_execution import ensure_spot_budget_cycle
        ensure_spot_budget_cycle(config_id, config, run_id, 'scheduled' if run_id.startswith('scheduled:') else 'manual')
    sync_langsmith_environment()
    initial_state = AgentState(
        symbol=symbol,
        messages=[],
        market_context={},
        account_context={},
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
                    "spot_cycle_id": run_id,
                    "run_id": run_id,
                }
            },
        )
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
        raise
