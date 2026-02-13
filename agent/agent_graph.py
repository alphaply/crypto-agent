import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from agent.agent_models import AgentState
from utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, \
    format_market_data_to_text
from utils.logger import setup_logger

try:
    from agent.agent_models import RealAgentOutput as RealAgentOutputSchema, \
        StrategyAgentOutput as StrategyAgentOutputSchema
except ModuleNotFoundError:
    from agent_models import RealAgentOutput as RealAgentOutputSchema, StrategyAgentOutput as StrategyAgentOutputSchema
from utils.prompt_utils import resolve_prompt_template, render_prompt

TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("AgentGraph")
import database
from utils.market_data import MarketTool
from config import config as global_config

load_dotenv()
# market_tool已移除，现在在每个节点中为交易对创建专属实例
PROJECT_ROOT = Path(__file__).resolve().parent


# ==========================================
# 3. Nodes (重点修改了 start_node)
# ==========================================


def _render_prompt(template: str, **kwargs) -> str:
    """安全渲染 Prompt，未提供的占位符默认置空。"""
    return template.format_map(defaultdict(str, kwargs))


def start_node(state: AgentState) -> AgentState:
    config_id = state.config_id
    symbol = state.symbol
    config = state.agent_config
    now = datetime.now(TZ_CN)
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now.weekday()]})"

    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    # 使用 config_id 作为 agent_name，实现完全隔离
    agent_name = config_id

    # 为该配置创建专属的MarketTool实例
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

    # --- 核心修改：完整提取新指标 ---
    indicators_summary = {}
    timeframes = ['1h', '4h', '1d', '1w'] if trade_mode == 'STRATEGY' else ['15m', '1h', '4h', '1d']

    raw_analysis = market_full.get("analysis", {})

    for tf in timeframes:
        if tf not in raw_analysis: continue
        tf_data = raw_analysis[tf]

        indicators_summary[tf] = {
            "price": tf_data.get("price"),
            "trend_status": tf_data.get("trend_status", "N/A"),  # 新增
            "recent_closes": tf_data.get("recent_closes", []),
            "recent_highs": tf_data.get("recent_highs", []),
            "recent_lows": tf_data.get("recent_lows", []),
            "ema": tf_data.get("ema"),
            "rsi": tf_data.get("rsi"),
            "atr": tf_data.get("atr"),
            # 新增指标
            "kdj": tf_data.get("kdj"),
            "macd": tf_data.get("macd"),
            "bollinger": tf_data.get("bollinger"),

            "volume_status": tf_data.get("volume_analysis", {}).get("status"),
            "vp": tf_data.get("vp", {})
        }

    market_context_llm = {
        "current_price": current_price,
        "atr_15m": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary
    }

    # 调用 Formatter
    formatted_market_data = format_market_data_to_text(market_context_llm)
    # 如果想用 Markdown 格式，也可以切换：
    # formatted_market_data = format_market_data_to_markdown(market_context_llm)

    history_entries = []
    if recent_summaries:
        for s in recent_summaries:
            ts = s.get('timestamp', 'Unknown')
            logic = s.get('strategy_logic') or s.get('content', '')
            # trend = s.get('market_trend', '')
            if "LLM Failed" in logic: continue
            # entry = f" [{ts}] Trend: {trend} Logic: {logic}"
            entry = f" [{ts}] Logic: {logic}"
            history_entries.append(entry)
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))
    prompt_template = resolve_prompt_template(config, trade_mode, PROJECT_ROOT, logger)
    leverage = global_config.get_leverage(config_id)

    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        # 增加 pos_side 字段的传递
        display_orders = [{
            "id": o.get('order_id'),
            "side": o.get('side'),
            "pos_side": o.get('pos_side'),  # <--- 传递这个关键字段
            "price": o.get('price'),
            "amount": o.get('amount')
        } for o in raw_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_orders)
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
    else:
        raw_mock_orders = account_data.get('mock_open_orders', [])
        display_mock_orders = [
            {"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'), "tp": o.get('take_profit'),
             "sl": o.get('stop_loss')} for o in raw_mock_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_mock_orders)

        system_prompt = render_prompt(
            prompt_template,
            model=config.get('model'),
            symbol=symbol,
            leverage=leverage,
            current_time=current_time_str,
            current_price=current_price,
            atr_15m=atr_15m,
            positions_text=positions_text,
            orders_text=orders_friendly_text,
            formatted_market_data=formatted_market_data,
            history_text=formatted_history_text
        )

    return AgentState(
        config_id=config_id,
        symbol=symbol,
        agent_config=config,
        market_context=market_full,
        account_context=account_data,
        history_context=recent_summaries,
        messages=[SystemMessage(content=system_prompt)],
        final_output={}
    )


def agent_node(state: AgentState) -> AgentState:
    config = state.agent_config
    symbol = state.symbol
    trade_mode = config.get('mode', 'STRATEGY').upper()

    logger.info(f"--- [Node] Agent: {config.get('model')} ({trade_mode}) ---")

    try:
        kwargs = {}
        if config.get('extra_body'):
            kwargs["extra_body"] = config.get('extra_body')

        output_schema = RealAgentOutputSchema if trade_mode == 'REAL' else StrategyAgentOutputSchema

        structured_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5),
            model_kwargs=kwargs
        ).with_structured_output(output_schema, method="function_calling")

        response = structured_llm.invoke(state.messages)
        return state.model_copy(update={"final_output": response.model_dump()})

    except Exception as e:
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        error_summary = {
            "market_trend": "Error", "key_levels": "N/A",
            "strategy_logic": f"LLM Failed: {str(e)}", "prediction": "Wait"
        }
        return state.model_copy(update={"final_output": error_summary})


def execution_node(state: AgentState) -> AgentState:
    # 保持原逻辑不变，此处省略以节省篇幅，请直接保留您原文件中的 execution_node 代码
    # ... (保持原代码不变) ...
    config_id = state.config_id
    symbol = state.symbol
    config = state.agent_config
    # 使用 config_id 作为 agent_name，实现完全隔离
    agent_name = config_id
    trade_mode = config.get('mode', 'STRATEGY').upper()

    # 为该配置创建专属的MarketTool实例
    market_tool = MarketTool(config_id=config_id)

    output = state.final_output
    if not output: return state

    summary = output.get('summary', {})
    raw_orders = output.get('orders', [])

    thought = summary.get('strategy_logic', '')
    predict = summary.get('prediction', '')
    trend = summary.get('market_trend', '')

    try:
        content = f"Trend: {trend}\nOutlook: {predict}"
        database.save_summary(symbol, agent_name, content, thought)
    except Exception as db_err:
        logger.warning(f"⚠️ [DB Error] Save summary failed: {db_err}")

    orders = sorted(raw_orders, key=lambda x: 0 if x['action'] == 'CANCEL' else (1 if x['action'] == 'CLOSE' else 2))

    def _is_duplicate_real_order(new_action, new_price, current_open_orders):
        if new_action not in ['BUY_LIMIT', 'SELL_LIMIT']: return False
        new_side = 'buy' if 'BUY' in new_action else 'sell'
        for existing in current_open_orders:
            if existing.get('side', '').lower() != new_side: continue
            exist_price = float(existing.get('price', 0))
            if exist_price > 0 and abs(exist_price - new_price) / exist_price < 0.001:
                return True
        return False

    for order in orders:
        action = (order.get('action') or '').upper()
        if action == 'NO_ACTION': continue
        log_reason = order.get('reason', '')

        if trade_mode == 'REAL':
            order['take_profit'] = 0
            order['stop_loss'] = 0

            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    market_tool.place_real_order(symbol, 'CANCEL', order, agent_name=agent_name)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}",
                                            trade_mode="REAL")

            elif action == 'CLOSE':
                market_tool.place_real_order(symbol, 'CLOSE', order, agent_name=agent_name)
                database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", order.get('entry_price'), 0, 0,
                                        log_reason, trade_mode="REAL")

            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                latest_account = market_tool.get_account_status(symbol, is_real=True, agent_name=agent_name)
                if _is_duplicate_real_order(action, entry_price, latest_account.get('real_open_orders', [])):
                    logger.info(f"🛑 [Filter] 忽略重复实盘挂单: {action} @ {entry_price}")
                    continue

                res = market_tool.place_real_order(symbol, action, order, agent_name=agent_name)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell',
                                            entry_price, 0, 0, log_reason, trade_mode="REAL")

        else:
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    database.cancel_mock_order(cancel_id)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel",
                                            trade_mode="STRATEGY")

            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                valid_hours = order.get('valid_duration_hours', 24)
                if valid_hours <= 0: valid_hours = 24

                expire_at = datetime.now() + timedelta(hours=valid_hours)
                expire_timestamp = expire_at.timestamp()

                side = 'BUY' if 'BUY' in action else 'SELL'
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"

                logger.info(f"💡 [STRATEGY] Idea: {side} @ {entry_price} | Expires in {valid_hours}h")

                database.create_mock_order(
                    symbol, side, entry_price, order.get('amount'),
                    order.get('stop_loss'), order.get('take_profit'),
                    agent_name=agent_name, order_id=mock_id, expire_at=expire_timestamp
                )

                database.save_order_log(
                    mock_id, symbol, agent_name, side, entry_price,
                    order.get('take_profit'), order.get('stop_loss'),
                    f"[Strategy] {log_reason} (Valid: {valid_hours}h)",
                    trade_mode="STRATEGY"
                )

    return state


# 5. Graph 编译与运行
workflow = StateGraph(AgentState)
workflow.add_node("start", start_node)
workflow.add_node("agent", agent_node)
workflow.add_node("execution", execution_node)
workflow.set_entry_point("start")
workflow.add_edge("start", "agent")
workflow.add_edge("agent", "execution")
workflow.add_edge("execution", END)
app = workflow.compile(name='Crypto Agent')


def run_agent_for_config(config: dict):
    config_id = config.get('config_id', 'unknown')
    symbol = config['symbol']
    mode_str = config.get('mode', 'STRATEGY').upper()
    logger.info(f"========================================================")
    logger.info(f"🚀 Launching Agent: [{config_id}] {symbol} | Model: {config.get('model')} | Mode: {mode_str}")
    logger.info(f"========================================================")

    initial_state = AgentState(
        config_id=config_id,
        symbol=symbol,
        messages=[],
        agent_config=config,
        market_context={},
        account_context={},
        history_context=[],
        final_output={}
    )

    try:
        app.invoke(initial_state)
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for [{config_id}] {symbol}: {e}")
        import traceback
        traceback.print_exc()
