import json
import os
import time
import math
import uuid
from typing import Annotated, List, TypedDict, Union, Dict, Any, Optional
from datetime import datetime

from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import pytz
from tool.logger import setup_logger
from tool.formatters import format_positions_to_agent_friendly, format_orders_to_agent_friendly, format_market_data_to_markdown

TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("AgentGraph")
import database 
from market_data import MarketTool

load_dotenv()
market_tool = MarketTool()


# A. 实盘执行模式 Prompt
REAL_TRADE_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **高胜率稳健合约交易员**。
当前时间: {current_time}
当前监控: {symbol} | 模式: 实盘交易 | 杠杆: {leverage}x
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【任务】
捕捉日内 结构清晰 的波段机会。你的目标是稳定盈利，而非频繁刷单。
**严禁追涨杀跌！** 你的优势在于耐心等待价格“犯错”，即等待价格快速插针到关键支撑/阻力位时进场。
如果市场出现符合策略的高盈亏比机会，你却因为过度犹豫而选择观望，将被视为严重失职。
如果当前价格悬在半空，没有到达你的伏击圈，请果断输出 **NO_ACTION**。
**实盘模式下，专注于优异的进场位置（Entry）与出场位置（Limit Close）。**
做单方式：双向持仓 做多做空均可

【权限与指令】
1. **BUY_LIMIT**: 左侧挂多。**价格必须低于{current_price}**（等待回调接多）。
2. **SELL_LIMIT**: 左侧挂空。**价格必须高于{current_price}**（等待反弹空）。
3. **CLOSE**: 挂限价单止盈/平仓。
4. **CANCEL**: 撤销已经失效或价格过远的挂单。
5. **NO_ACTION**: 价格未到理想点位，继续空仓等待。

【决策铁律】
1. **拒绝平庸点位**: 
   - 严禁在当前价格 0.1% 范围内挂入场单（除非是极其强势的突破回踩）。
2. **ATR 距离约束**:
   - 挂单价格距离现价通常应至少保留 **0.5倍 ~ 1.5倍 的 15m ATR** 的空间。
   - 示例: 如果现价 100，ATR 是 2，不要挂 99.8，要挂 99.0 或更低。
3. **左侧思维**: 
   - 想象你是在并在价格下跌时买入，在价格上涨时卖出。不要顺着当前秒级的波动去追。
4. **防滑点**: 严禁使用市价开仓，必须使用 Limit 单。
5. 仅在信心 > 75% 且盈亏比极佳时出手。

【资金与持仓】
可用余额: {balance:.2f} USDT

现有持仓: 
{positions_text}

活跃挂单 (Active Orders): 
{orders_text}

【全量市场数据】
{formatted_market_data}

【历史思路回溯 (Context)】
以下是最近 3 次的分析记录，请参考过去的时间线和思路演变：
----------------------------------------
{history_text}
----------------------------------------

【输出要求】
1. **时效性检查**: 现在的价格是否已经破位？之前的挂单是否需要 CANCEL？
2. **价格计算**: 
   - BUY_LIMIT 建议稍微埋深一点
   - SELL_LIMIT 建议稍微挂高一点
3. 禁止梭哈，单笔下单金额不得超过 可用余额 的 50%。

思路 解读 中文描述
- `action`: BUY_LIMIT / SELL_LIMIT / CLOSE / CANCEL / NO_ACTION
- `pos_side`: 如果是 CLOSE，必须填 'LONG' 或 'SHORT'；其他情况留空
- `entry_price`: 挂单价格 / 平仓价格 (CLOSE 必须填此项)
- `amount`: 下单数量(币的个数，非 USDT 金额)
- `reason`: 简短的执行理由
- `take_profit`: 填 0
- `stop_loss`: 填 0
- `cancel_order_id`: 填要撤销的订单 ID (如8389766084576502933)
"""

STRATEGY_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **机构级加密货币策略师 (Institutional Crypto Strategist)**。
当前时间: {current_time}
监控标的: {symbol} | 周期视角: 4H/1D (中长线波段)
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【核心任务】
基于**市场结构 (Structure)、供需区 (Supply/Demand) 及流动性掠夺 (Liquidity Sweep)**，制定高盈亏比 (R/R > 2.0) 的挂单计划。
**宁缺毋滥**：仅在出现明确的结构性反转或回踩信号时入场。

【全量市场数据】
{formatted_market_data}

【账户状态】
[实盘持仓] (参考用，人类可能没有实际按照你的策略进行下单):
{positions_text}

[活跃策略挂单] (需管理):
{orders_text}

【历史分析回溯】
{history_text}

【决策思维链】
1. **趋势研判**: 结合 EMA 与 K 线形态，确认当前是大周期(4H/1D)的上升、下降还是震荡结构。
2. **位置筛选**: 寻找关键的 Order Block (OB) 或 FVG (Fair Value Gap) 作为入场点，**严禁追涨杀跌**。
3. **订单管理 (关键)**: 
   - 检查上方【活跃策略挂单】。
   - 如果挂单逻辑已失效（如价格已远离、结构已破坏、或有更好的点位），必须输出 `CANCEL` 指令。
   - 如果时间过于久远，那么也是需要进行重新评估有效性
   - 如果现有挂单依然完美，输出 `NO_ACTION`。
4. **新单构建**: 仅在信心 > 80% 时生成新的 `BUY/SELL_LIMIT`，必须带严格的 `stop_loss` 和 `take_profit`。

【输出约束】
- `summary`: 简述趋势与关键位。
- `orders`: 
    - 必须使用 **LIMIT** 单 (策略模式不支持市价)。
    - **R/R 计算**: (止盈-入场)/(入场-止损) 必须 > 2。
    - **CANCEL**: 填入具体的 `cancel_order_id`。
"""


class OrderParams(BaseModel):
    """交易指令结构"""
    reason: str = Field(description="简短的决策理由")
    action: str = Field(
        description="动作: 'BUY_LIMIT', 'SELL_LIMIT', 'CANCEL', 'CLOSE', 'NO_ACTION'",
        pattern="^(BUY_LIMIT|SELL_LIMIT|CANCEL|CLOSE|NO_ACTION)$"
    )
    pos_side: str = Field(description="平仓方向: 仅在 CLOSE 时必填，填 'LONG' (平多) 或 'SHORT' (平空)", default="")
    cancel_order_id: str = Field(description="撤单时填入 对应的ID（如8389766084576502933）", default="")
    entry_price: float = Field(description="挂单价格 (CLOSE 时为平仓价格)", default=0.0)
    amount: float = Field(description="下单数量 (币的个数，非 USDT 金额)", default=0.0)
    take_profit: float = Field(description="止盈价格", default=0.0)
    stop_loss: float = Field(description="止损价格", default=0.0)

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    key_levels: str = Field(description="关键支撑与阻力位")
    current_trend: str = Field(description="趋势分析推断")
    strategy_thought: str = Field(description="详细的思维链行情分析")
    predict: str = Field(description="对未来行情的预测")

class AgentOutput(BaseModel):
    """LLM 最终输出结构"""
    summary: MarketSummaryParams
    orders: List[OrderParams]

class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    final_output: Dict[str, Any]



def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    now = datetime.now(TZ_CN)
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now.weekday()]})"
    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    
    logger.info(f"--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        # 获取全量数据
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode)
        # 获取账户数据 (实盘模式读交易所，策略模式读数据库或模拟余额)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec)
        # 获取最近历史记录
        recent_summaries = database.get_recent_summaries(symbol, limit=3)
    except Exception as e:
        logger.error(f"❌ [Data Fetch Error]: {e}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        recent_summaries = []
    
    # 资金计算
    leverage = int(os.getenv('LEVERAGE', 10))
    balance = account_data.get('balance', 0)
    
    # 市场数据解析
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0
    
    # 构建 Market Context
    indicators_summary = {}
    for tf in ['5m', '15m', '1h', '4h', '1d','1w']:
        tf_data = market_full.get("analysis", {}).get(tf)
        if tf_data:
            vp_data = tf_data.get("vp", {})
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "recent_closes": tf_data.get("recent_closes", [])[-5:],
                "recent_highs": tf_data.get("recent_highs", [])[-5:],
                "recent_lows": tf_data.get("recent_lows", [])[-5:],
                "rsi": tf_data.get("rsi"),
                "atr": tf_data.get("atr"),
                "ema": tf_data.get("ema"),
                "volume_status": tf_data.get("volume_analysis", {}).get("status"),
                "vp": {
                    "poc": vp_data.get("poc"), 
                    "vah": vp_data.get("vah"), 
                    "val": vp_data.get("val"), 
                    "hvns": vp_data.get("hvns", []) 
                }
            }

    market_context_llm = {
        "current_price": current_price,
        "atr_15m": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary 
    }
    
    formatted_market_data = format_market_data_to_markdown(market_context_llm)
    
    # 历史数据结构化处理
    history_entries = []
    if recent_summaries:
        for s in recent_summaries:
            ts = s['timestamp'] if 'timestamp' in s else 'Unknown'
            agent = s['agent_name'] if 'agent_name' in s else 'Unknown'
            logic = s['strategy_logic'] if 'strategy_logic' in s else 'No Logic'
            if "LLM Failed" in logic or "json_invalid" in logic:
                continue 
                
            content = s['content'][:200] + "..." if len(s['content']) > 200 else s['content']
            logic = logic[:300] + "..." if len(logic) > 300 else logic
            entry = f" [{ts}] {agent}: {content} | Logic: {logic}"
            history_entries.append(entry)
        formatted_history_text = "\n".join(history_entries)
    else:
        formatted_history_text = "(暂无历史记录)"

    # 格式化持仓文本
    positions_text = format_positions_to_agent_friendly(account_data.get('real_positions', []))

    # 根据模式选择 Prompt
    if is_real_exec:
        # --- 实盘模式 ---
        raw_orders = account_data.get('real_open_orders', [])
        # 构建显示用对象列表（保持原有逻辑，用于 format 函数）
        display_orders = [{
            "id": o.get('order_id') or o.get('id'), # 兼容不同 key
            "side": o.get('side'), 
            "type": o.get('type'), 
            "price": o.get('price'), 
            "amount": o.get('amount')
        } for o in raw_orders]
        
        # 使用新函数转为 Friendly String
        orders_friendly_text = format_orders_to_agent_friendly(display_orders)
        
        system_prompt = REAL_TRADE_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            leverage=leverage,
            current_time=current_time_str,
            current_price=market_context_llm['current_price'],
            atr_15m=market_context_llm['atr_15m'],
            balance=balance,
            positions_text=positions_text,
            orders_text=orders_friendly_text, # 传入文本
            formatted_market_data=formatted_market_data,
            history_text=formatted_history_text,
        )
    else:
        # --- 策略模式 ---
        raw_mock_orders = account_data.get('mock_open_orders', [])
        display_mock_orders = [{
            "id": o.get('order_id') or o.get('id'), 
            "side": o.get('side'), 
            "type": "LIMIT",
            "price": o.get('price'), 
            "amount": o.get('amount'),
            "tp": o.get('take_profit'), 
            "sl": o.get('stop_loss')
        } for o in raw_mock_orders]

        # 使用新函数转为 Friendly String
        orders_friendly_text = format_orders_to_agent_friendly(display_mock_orders)

        system_prompt = STRATEGY_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            current_time=current_time_str,
            current_price=market_context_llm['current_price'],
            atr_15m=market_context_llm['atr_15m'],
            positions_text=positions_text,
            orders_text=orders_friendly_text, # 传入文本
            formatted_market_data=formatted_market_data,
            history_text=formatted_history_text,
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
    logger.info(f"--- [Node] Agent: {config.get('model')} is thinking for {symbol} ---")
    
    try:
        current_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5) 
        ).with_structured_output(AgentOutput,method="function_calling")
        
        response = current_llm.invoke(state['messages'])
        return {**state, "final_output": response.model_dump()}
        
    except Exception as e:
        logger.error(f"❌ [LLM Error] ({symbol}): {e}")
        error_output = {
            "summary": {
                "current_trend": "Error", "key_levels": "N/A", 
                "strategy_thought": f"LLM Failed: {str(e)}", "predict": "Wait"
            }, 
            "orders": []
        }
        return {**state, "final_output": error_output}

def execution_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    agent_name = config.get('model', 'Unknown')
    
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    logger.info(f"--- [Node] Execution: {symbol} | Mode: {trade_mode} ---")
    
    output = state['final_output']
    if not output: return state

    summary = output.get('summary', {})
    raw_orders = output.get('orders', [])
    
    orders = sorted(raw_orders, key=lambda x: 0 if x['action']=='CANCEL' else (1 if x['action']=='CLOSE' else 2))
    
    content = f"Trend: {summary.get('current_trend')}\nKey Levels: {summary.get('key_levels')}\nPredict: {summary.get('predict')}"
    try:
        database.save_summary(symbol, agent_name, content, summary.get('strategy_thought'))
    except Exception as db_err:
        logger.warning(f"⚠️ [DB Error] Save summary failed: {db_err}")

    def _is_duplicate_order(new_action, new_price, current_open_orders):
        """
        检查是否有雷同挂单
        逻辑：方向相同，且价格差异 < 0.1%
        """
        if new_action not in ['BUY_LIMIT', 'SELL_LIMIT']:
            return False
            
        new_side = 'buy' if 'BUY' in new_action else 'sell'
        
        for existing in current_open_orders:
            # 检查方向
            exist_side = existing.get('side', '').lower()
            if exist_side != new_side:
                continue
            
            # 检查价格 (容错率 0.1%)
            exist_price = float(existing.get('price', 0))
            if exist_price > 0 and abs(exist_price - new_price) / exist_price < 0.001:
                return True # 判定为重复
        return False

    # 2. 执行/记录订单
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': continue
        
        log_reason = order.get('reason', '')

        # 分支 A: 实盘执行 (REAL)
        if trade_mode == 'REAL':
            # 强制清空 TP/SL (实盘暂由人工或另外逻辑控制风控)
            order['take_profit'] = 0
            order['stop_loss'] = 0
            
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    logger.info(f"🔄 [REAL] Cancel: {cancel_id}")
                    market_tool.place_real_order(symbol, 'CANCEL', order)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}", trade_mode="REAL")

            # 2. 平仓
            elif action == 'CLOSE':
                logger.info(f"🎯 [REAL] Close Position (Limit)")
                close_res = market_tool.place_real_order(symbol, 'CLOSE', order)
                if close_res:
                     database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", order.get('entry_price'), 0, 0, log_reason, trade_mode="REAL")

            # 3. 开仓 (Limit) - 
            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                
                # 在下单前获取最新的实盘挂单，而不是使用旧的缓存数据
                latest_account_data = market_tool.get_account_status(symbol, is_real=True)
                real_open_orders = latest_account_data.get('real_open_orders', [])
                
                if _is_duplicate_order(action, entry_price, real_open_orders):
                    logger.info(f"🛑 [Filter] 忽略重复实盘挂单: {action} @ {entry_price}")
                    continue # 跳过下单

                logger.info(f"🚀 [REAL] Order: {action} @ {entry_price}")
                res = market_tool.place_real_order(symbol, action, order)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', 
                                            entry_price, 0, 0, log_reason, trade_mode="REAL")

        # 分支 B: 策略模式 (STRATEGY)
        else:
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    try:
                        logger.info(f"🔄 [STRATEGY] Cancelling Mock Order: {cancel_id}")
                        database.cancel_mock_order(cancel_id)
                        database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel: {cancel_id}", trade_mode="STRATEGY")
                    except Exception as e:
                        logger.warning(f"⚠️ [Mock Cancel Error]: {e}")

            # 2. 开仓 - ✅ 增加防重检测
            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                
                # 在下单前获取最新的策略挂单，而不是使用旧的缓存数据
                # latest_account_data = market_tool.get_account_status(symbol, is_real=False)
                # mock_open_orders = latest_account_data.get('mock_open_orders', [])

                # if _is_duplicate_order(action, entry_price, mock_open_orders):
                #     logger.info(f"🛑 [Filter] 忽略重复策略挂单: {action} @ {entry_price}")
                #     continue # 跳过入库

                side = 'BUY' if 'BUY' in action else 'SELL'
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                
                logger.info(f"💡 [STRATEGY] Idea: {side} @ {entry_price} | ID: {mock_id}")
                
                database.create_mock_order(
                    symbol, side, 
                    entry_price, 
                    order['amount'], 
                    order['stop_loss'], 
                    order['take_profit'],
                    order_id=mock_id 
                )

                database.save_order_log(
                    mock_id, symbol, agent_name, side, 
                    entry_price, 
                    order.get('take_profit'), 
                    order.get('stop_loss'), 
                    f"[Strategy] {log_reason}",
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
    """
    运行 Agent 的主入口函数
    """
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