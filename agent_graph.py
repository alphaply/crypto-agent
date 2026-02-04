import json
import os
import time
import math
import uuid
from typing import Annotated, List, TypedDict, Union, Dict, Any, Optional
from datetime import datetime, timedelta

from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from typing import Literal
from dotenv import load_dotenv
import pytz
from utils.logger import setup_logger
from utils.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, format_market_data_to_markdown, format_market_data_to_text
from prompts import PROMPT_MAP  # 确保 prompts.py 存在或在此处定义

TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("AgentGraph")
import database 
from market_data import MarketTool

load_dotenv()
market_tool = MarketTool()

# ==========================================
# 1. 定义 Schema (字段统一，描述分离)
# ==========================================

# --- 实盘模式 Schema ---
class RealOrderParams(BaseModel):
    """实盘交易指令：注重精确执行与平仓逻辑"""
    reason: str = Field(description="简短的执行理由")
    action: Literal['BUY_LIMIT', 'SELL_LIMIT', 'CLOSE', 'CANCEL', 'NO_ACTION'] = Field(
        description="实盘动作。CLOSE用于平仓，LIMIT用于挂单"
    )
    pos_side: Optional[Literal['LONG', 'SHORT']] = Field(description="平仓方向: CLOSE时必填", default=None)
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="挂单价格/平仓价格", default=0.0)
    amount: float = Field(description="下单数量", default=0.0)

class RealMarketSummary(BaseModel):
    """实盘分析总结"""
    market_trend: str = Field(description="当前短期市场微观趋势与动能")
    key_levels: str = Field(description="日内关键支撑位与阻力位")
    strategy_logic: str = Field(description="当前持仓的风控评估、浮盈浮亏分析与执行逻辑")
    prediction: str = Field(description="短期价格行为(Price Action)预判")

class RealAgentOutput(BaseModel):
    summary: RealMarketSummary
    orders: List[RealOrderParams]

# --- 策略模式 Schema ---
class StrategyOrderParams(BaseModel):
    """策略模拟指令：注重盈亏比、计划性与时效性"""
    reason: str = Field(description="策略逻辑与盈亏比分析 (例如 R/R: 3.2)")
    action: Literal['BUY_LIMIT', 'SELL_LIMIT', 'CANCEL', 'NO_ACTION'] = Field(
        description="策略动作。策略模式下通常不主动调用 CLOSE，而是依赖 TP/SL 触发"
    )
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="入场挂单价格", default=0.0)
    amount: float = Field(description="模拟下单数量", default=0.0)
    take_profit: float = Field(description="计划止盈位 (必须设置)", default=0.0)
    stop_loss: float = Field(description="计划止损位 (必须设置)", default=0.0)
    
    valid_duration_hours: int = Field(
        description="挂单有效期(小时)。例如填4，代表4小时后如果未成交则自动撤单。填0代表24小时。", 
        default=24
    )

class StrategyMarketSummary(BaseModel):
    """策略分析总结"""
    market_trend: str = Field(description="4H/1D 宏观趋势分析")
    key_levels: str = Field(description="市场结构(Structure)、供需区与流动性分布")
    strategy_logic: str = Field(description="详细的策略思维链、盈亏比逻辑与挂单失效条件")
    prediction: str = Field(description="未来走势推演与剧本规划")

class StrategyAgentOutput(BaseModel):
    summary: StrategyMarketSummary
    orders: List[StrategyOrderParams]


# ==========================================
# 2. State 定义
# ==========================================
class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    final_output: Dict[str, Any]


# ==========================================
# 3. Nodes
# ==========================================

def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    now = datetime.now(TZ_CN)
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now.weekday()]})"
    
    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    agent_name = config.get('model', 'Unknown_Agent')
    
    logger.info(f"--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        # 获取全量数据
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode)
        # 获取账户数据
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec,agent_name=agent_name)
        # 获取最近历史记录
        recent_summaries = database.get_recent_summaries(symbol, limit=3)
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        recent_summaries = []

    # 实盘特有逻辑：记录快照与同步成交
    if is_real_exec:
        try:
            balance = account_data.get('balance', 0)
            positions = account_data.get('real_positions', [])
            total_unrealized_pnl = sum([float(p.get('unrealized_pnl', 0)) for p in positions])
            database.save_balance_snapshot(symbol, balance, total_unrealized_pnl)
            
            recent_trades = market_tool.fetch_recent_trades(symbol, limit=10)
            if recent_trades:
                database.save_trade_history(recent_trades)
                logger.info(f"🔄 [Data] Synced {len(recent_trades)} trades from exchange.")
        except Exception as e:
            logger.error(f"❌ Failed to save real-time stats: {e}")

    # 准备 Prompt 变量
    balance = account_data.get('balance', 0)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0
    
    # 格式化市场数据
    indicators_summary = {}
    for tf in ['5m', '15m', '1h', '4h', '1d', '1w']:
        tf_data = market_full.get("analysis", {}).get(tf)
        if tf_data:
            vp_data = tf_data.get("vp", {})
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "recent_closes": tf_data.get("recent_closes", [])[-5:],
                "ema": tf_data.get("ema"),
                "rsi": tf_data.get("rsi"),
                "atr": tf_data.get("atr"),
                "volume_status": tf_data.get("volume_analysis", {}).get("status"),
                "vp": {"poc": vp_data.get("poc"), "vah": vp_data.get("vah"), "val": vp_data.get("val"), "hvns": vp_data.get("hvns", [])}
            }

    market_context_llm = {
        "current_price": current_price,
        "atr_15m": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary 
    }
    formatted_market_data = format_market_data_to_text(market_context_llm)
    
    # 格式化历史记录
    history_entries = []
    if recent_summaries:
        for s in recent_summaries:
            ts = s.get('timestamp', 'Unknown')
            # 兼容旧数据：如果是旧字段 content/logic，如果是新字段 strategy_logic
            logic = s.get('strategy_logic') or s.get('content', '')
            if "LLM Failed" in logic: continue 
            entry = f" [{ts}] Logic: {logic[:100]}..."
            history_entries.append(entry)
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))

    # --- Prompt 选择与构建 ---
    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [{"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'), "amount": o.get('amount')} for o in raw_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_orders)
        
        system_prompt = PROMPT_MAP.get("REAL").format(
            model=config.get('model'),
            symbol=symbol,
            leverage=int(os.getenv('LEVERAGE', 10)),
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
        display_mock_orders = [{"id": o.get('order_id'), "side": o.get('side'), "price": o.get('price'), "tp": o.get('take_profit'), "sl": o.get('stop_loss')} for o in raw_mock_orders]
        orders_friendly_text = format_orders_to_agent_friendly(display_mock_orders)

        system_prompt = PROMPT_MAP.get("STRATEGY").format(
            model=config.get('model'),
            symbol=symbol,
            current_time=current_time_str,
            current_price=current_price,
            atr_15m=atr_15m,
            positions_text=positions_text,
            orders_text=orders_friendly_text,
            formatted_market_data=formatted_market_data,
            history_text=formatted_history_text
        )

    return {
        "symbol": symbol,
        "agent_config": config,
        "market_context": market_full,
        "account_context": account_data,
        "history_context": recent_summaries,
        "messages": [SystemMessage(content=system_prompt)],
        "final_output": {}
    }


def agent_node(state: AgentState) -> AgentState:
    config = state['agent_config']
    symbol = state['symbol']
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    logger.info(f"--- [Node] Agent: {config.get('model')} ({trade_mode}) ---")
    
    try:
        kwargs = {}
        if config.get('extra_body'):
            kwargs["extra_body"] = config.get('extra_body')

        # 动态选择 Output Schema
        output_schema = RealAgentOutput if trade_mode == 'REAL' else StrategyAgentOutput

        structured_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5),
            model_kwargs=kwargs
        ).with_structured_output(output_schema, method="function_calling")
        
        response = structured_llm.invoke(state['messages'])
        return {**state, "final_output": response.model_dump()}
        
    except Exception as e:
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        # 构造一个符合 Schema 的空错误对象，避免 Execution 节点崩溃
        error_summary = {
            "market_trend": "Error", "key_levels": "N/A", 
            "strategy_logic": f"LLM Failed: {str(e)}", "prediction": "Wait"
        }
        return {**state, "final_output": {"summary": error_summary, "orders": []}}


def execution_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    agent_name = config.get('model', 'Unknown')
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    output = state['final_output']
    if not output: return state

    summary = output.get('summary', {})
    raw_orders = output.get('orders', [])
    
    # 1. 保存分析 Summary (字段已统一，直接读取)
    # 映射逻辑：将 summary 的核心字段存入 DB
    thought = summary.get('strategy_logic', '')
    predict = summary.get('prediction', '')
    trend = summary.get('market_trend', '')
    
    try:
        # content 字段存放 趋势+预测，strategy_logic 存放详细思路
        content = f"[{trade_mode}] Trend: {trend}\nOutlook: {predict}"
        database.save_summary(symbol, agent_name, content, thought)
    except Exception as db_err:
        logger.warning(f"⚠️ [DB Error] Save summary failed: {db_err}")

    # 排序：撤单优先 -> 平仓 -> 开仓
    orders = sorted(raw_orders, key=lambda x: 0 if x['action']=='CANCEL' else (1 if x['action']=='CLOSE' else 2))

    # 辅助：防重检查
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
        action = order.get('action', '').upper()
        if action == 'NO_ACTION': continue
        log_reason = order.get('reason', '')

        # ==========================================
        # 分支 A: 实盘执行 (REAL)
        # ==========================================
        if trade_mode == 'REAL':
            # 实盘暂由人工/独立风控模块控制 TP/SL，此处保持 Limit 单纯净
            order['take_profit'] = 0
            order['stop_loss'] = 0
            
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    market_tool.place_real_order(symbol, 'CANCEL', order,agent_name=agent_name)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}", trade_mode="REAL")

            elif action == 'CLOSE':
                market_tool.place_real_order(symbol, 'CLOSE', order,agent_name=agent_name)
                database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", order.get('entry_price'), 0, 0, log_reason, trade_mode="REAL")

            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                # 实时防重
                latest_account = market_tool.get_account_status(symbol, is_real=True,agent_name=agent_name)
                if _is_duplicate_real_order(action, entry_price, latest_account.get('real_open_orders', [])):
                    logger.info(f"🛑 [Filter] 忽略重复实盘挂单: {action} @ {entry_price}")
                    continue

                res = market_tool.place_real_order(symbol, action, order,agent_name=agent_name)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', entry_price, 0, 0, log_reason, trade_mode="REAL")

        # ==========================================
        # 分支 B: 策略模式 (STRATEGY)
        # ==========================================
        else:
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    database.cancel_mock_order(cancel_id)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel", trade_mode="STRATEGY")

            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                
                # 🔥 计算过期时间 (Execution Node 补全逻辑)
                valid_hours = order.get('valid_duration_hours', 24)
                if valid_hours <= 0: valid_hours = 24
                
                expire_at = datetime.now() + timedelta(hours=valid_hours)
                expire_timestamp = expire_at.timestamp()

                side = 'BUY' if 'BUY' in action else 'SELL'
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                
                logger.info(f"💡 [STRATEGY] Idea: {side} @ {entry_price} | Expires in {valid_hours}h")
                
                # 传入 expire_at
                database.create_mock_order(
                    symbol, side, 
                    entry_price, 
                    order.get('amount'), 
                    order.get('stop_loss'), 
                    order.get('take_profit'),
                    order_id=mock_id,
                    expire_at=expire_timestamp 
                )

                database.save_order_log(
                    mock_id, symbol, agent_name, side, 
                    entry_price, 
                    order.get('take_profit'), 
                    order.get('stop_loss'), 
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
app = workflow.compile()

def run_agent_for_config(config: dict):
    symbol = config['symbol']
    mode_str = config.get('mode', 'STRATEGY').upper()
    
    logger.info(f"========================================================")
    logger.info(f"🚀 Launching Agent: {symbol} | Model: {config.get('model')} | Mode: {mode_str}")
    logger.info(f"========================================================")

    initial_state: AgentState = {
        "symbol": symbol,
        "messages": [],
        "agent_config": config,
        "market_context": {},
        "account_context": {},
        "history_context": [],
        "final_output": {}
    }

    try:
        app.invoke(initial_state)
    except Exception as e:
        logger.error(f"❌ Critical Graph Error for {symbol}: {e}")
        import traceback
        traceback.print_exc()