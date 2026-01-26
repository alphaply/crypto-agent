import json
import os
import time
import math
import uuid
from typing import Annotated, List, TypedDict, Union, Dict, Any, Optional
from datetime import datetime
import pytz  # 需要确保安装 pytz 库

# LangChain / LangGraph Imports
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# 自定义模块
import database
from market_data import MarketTool

# 加载环境变量
load_dotenv()
market_tool = MarketTool()

# ==========================================
# 0. Prompt 模板定义
# ==========================================

# A. 实盘执行模式 Prompt (支持 TP/SL 管理)
REAL_TRADE_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **专业合约交易员 (Execution Trader)**。
当前时间: **{system_time}**
当前监控: **{symbol}** | 模式: 🔴 实盘交易 (REAL EXECUTION) | 杠杆: {leverage}x
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
每15m或者1h盯盘一次。
1. **合约双向交易**: 这是一个USDT永续合约账户，你可以根据行情灵活地 **做多 (Long)** 或 **做空 (Short)**。
2. **目标**: 捕捉**高胜率**的日内波段，并保护现有账户资金。
3. **权限**: 你拥有完整权限，既可以开新仓，也可以对现有持仓进行 **限价止盈 (平仓)** 或 **止损 (平仓)**。

【现有持仓状态】
{positions_json}
*(如果没有持仓，请专注于寻找入场机会；如果有持仓，请评估是否需要设置 TP/SL 或 平仓)*

【权限与指令 (Action Space)】
1. **开仓指令 (Open Positions)**:
   - `BUY_LIMIT`: 挂单做多 (价格 < 现价)。
   - `SELL_LIMIT`: 挂单做空 (价格 > 现价)。
   
2. **持仓管理 (Manage Positions - 平仓)**:
   *以下指令用于对现有持仓进行平仓操作（平多或平空）*
   - `ADD_TP`: 设置限价止盈单 (Limit Reduce-Only)。若持多单则卖出止盈，持空单则买入止盈。
   - `ADD_SL`: 设置止损单 (Stop Market/Limit)。若持多单则卖出止损，持空单则买入止损。
   - `CLOSE`: 市价全平当前持仓。
   
3. **订单管理**:
   - `CANCEL`: 撤销指定的未成交挂单。
   
4. **观望**:
   - `NO_ACTION`: 没有高把握或无需调整时保持静默。

【决策铁律】
1. **点位精准**: 开仓必须使用 Limit 单，严禁追涨杀跌。
2. **风控优先**: 如果持有仓位且未设置止损，必须优先考虑使用 `ADD_SL` 保护头寸。
3. **止盈策略**: 如果持仓已有浮盈，建议使用 `ADD_TP` 在关键阻力/支撑位分批平仓止盈。
4. **趋势顺势**: 尊重 1H/4H 大级别趋势，不要在暴跌中盲目接飞刀。
5. 仅在信心 > 75% 时执行开仓。

【资金与持仓】
可用余额: {balance:.2f} USDT
活跃挂单: {orders_json}

【全量市场数据】
{formatted_market_data}

【历史思路回溯】
{history_text}

【逻辑校验】
1. 如果 `action` 是 `ADD_TP` 或 `ADD_SL`，你必须持有仓位，且 `amount` 不得超过持仓数量。
2. `BUY_LIMIT` 价格必须 < {current_price}。
3. `SELL_LIMIT` 价格必须 > {current_price}。

【输出要求】
思路 解读 中文描述。
请输出 JSON，包含 `orders` 列表。
- `action`: BUY_LIMIT / SELL_LIMIT / ADD_TP / ADD_SL / CLOSE / CANCEL / NO_ACTION
- `entry_price`: 挂单价格 (如果是 SL，填触发价格)
- `amount`: 数量 (币的个数)
- `reason`: 执行理由
- `cancel_order_id`: 仅在 CANCEL 时填写
"""

# B. 策略模式 Prompt (稳健中长线)
STRATEGY_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **资深加密货币宏观策略师 (Macro Strategist)**。
当前时间: **{system_time}**
当前监控: **{symbol}** | 模式: 🔵 策略分析 (STRATEGY IDEA)
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
每1h盯盘一次。
你需要通过 **4小时 (4H) 及 日线 (1D)** 级别分析市场，制定稳健的**中长线趋势交易计划**。
注意：这是一个合约市场，你可以根据结构位建议 **做多 (Long)** 或 **做空 (Short)**。
**严禁**关注 5分钟/15分钟 的短期噪音。你的目标是捕捉几百点以上的大幅波段，而非日内刷单。

【策略核心要求】
1. **时间框架**: 必须以 4H 结构位、1D 供需区、周线级别支撑阻力为核心依据。
2. **盈亏比 (R/R)**: 预期 R/R 必须 **> 3.0**。如果盈亏比不佳，宁可空仓。
3. **稳健入场**: 
   - 等待关键位置的“假突破回踩”或“趋势线共振”。
   - 不要在这个模式下尝试激进的左侧接针，除非是日线级别的强支撑。
4. **完整性**: 必须明确给出 入场价、止损价 (SL)、止盈价 (TP)。

【动态调整】
- 如果之前的策略挂单逻辑已失效（如趋势反转或价格长期未成交），请输出 `CANCEL` 指令清理旧单。
- 仅在信心 > 85% 且符合大周期趋势时出手。

【当前状态】
现有持仓: {positions_json}
活跃策略挂单: {orders_json}

【全量市场数据】
{formatted_market_data}

【历史思路回溯】
{history_text}

【输出要求】
思路 解读 中文描述。
请输出 JSON。
- `action`: BUY_LIMIT / SELL_LIMIT / CANCEL / NO_ACTION
- `cancel_order_id`: 撤单 ID
- `entry_price`: 建议入场价
- `take_profit`: 建议止盈价 (目标位)
- `stop_loss`: 建议止损价 (失效位)
- `reason`: 详细的策略逻辑，必须包含对 4H/1D 结构的分析。
"""

# ==========================================
# 1. 定义 Pydantic 输出结构 (Schema)
# ==========================================

class OrderParams(BaseModel):
    """交易指令结构"""
    reason: str = Field(description="简短的决策理由")
    action: str = Field(
        description="动作: 'BUY_LIMIT', 'SELL_LIMIT', 'ADD_TP', 'ADD_SL', 'CANCEL', 'CLOSE', 'NO_ACTION'",
        pattern="^(BUY_LIMIT|SELL_LIMIT|ADD_TP|ADD_SL|CANCEL|CLOSE|NO_ACTION)$"
    )
    cancel_order_id: str = Field(description="撤单时填入 ID，否则留空", default="")
    entry_price: float = Field(description="挂单价格 / TP价格 / SL触发价格")
    amount: float = Field(description="下单数量 (币的个数)，如果是 TP/SL 建议填 0 表示全仓", default=0.0)
    take_profit: float = Field(description="策略模式下的止盈价格", default=0.0)
    stop_loss: float = Field(description="策略模式下的止损价格", default=0.0)

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    current_trend: str = Field(description="趋势判断 (4H/1D)")
    key_levels: str = Field(description="关键支撑与阻力位")
    strategy_thought: str = Field(description="详细的思维链分析")
    predict: str = Field(description="对未来行情的预测与建议")

class AgentOutput(BaseModel):
    """LLM 最终输出结构"""
    summary: MarketSummaryParams
    orders: List[OrderParams]

# ==========================================
# 2. 定义 Graph 状态 (State)
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
# 3. 核心工具函数：数据转 Markdown
# ==========================================

def format_market_data_to_markdown(data: dict) -> str:
    """
    将复杂的市场 JSON 数据转换为 LLM 易读的 Markdown 格式
    支持动态 Timeframes
    """
    def fmt_price(price):
        if price is None or price == 0: return "0"
        abs_p = abs(price)
        if abs_p >= 1000: return f"{int(price)}"      
        if abs_p >= 1: return f"{price:.2f}"          
        return f"{price:.4f}"

    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    oi_str = fmt_num(sent.get("open_interest", 0))
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    
    header = (
        f"**Snapshot** | Price: {fmt_price(current_price)}\n"
        f"Sentiment: Fund: {funding:.4f}% | OI: {oi_str} | Vol24h: {vol_24h}\n"
    )

    table_header = (
        "| TF | RSI | EMA (20/50/100/200) | HVN (Key Levels) |\n"
        "|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    
    # 获取数据中存在的周期键值 (动态)
    available_tfs = list(indicators.keys())
    # 定义排序顺序，确保输出整齐
    sort_order = ['5m', '15m', '1h', '4h', '12h', '1d', '3d', '1w']
    available_tfs.sort(key=lambda x: sort_order.index(x) if x in sort_order else 99)

    for tf in available_tfs:
        d = indicators[tf]
        rsi = f"{d.get('rsi', 0):.1f}"
        
        ema = d.get('ema', {})
        ema_str = f"{fmt_price(ema.get('ema_20', 0))}/{fmt_price(ema.get('ema_50', 0))}/{fmt_price(ema.get('ema_100', 0))}/{fmt_price(ema.get('ema_200', 0))}"
        
        vp = d.get('vp', {})
        # 取前3个高筹码区
        raw_hvns = vp.get('hvns', [])
        hvn_str = ",".join([fmt_price(h) for h in sorted(raw_hvns, reverse=True)[:3]])
        
        row = f"| {tf} | {rsi} | {ema_str} | {hvn_str} |"
        rows.append(row)
    
    return header + table_header + "\n".join(rows)

# ==========================================
# 4. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    
    print(f"\n--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        # 1. 传入 mode 让 market_tool 拉取正确周期
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec)
        recent_summaries = database.get_recent_summaries(symbol, limit=3)
    except Exception as e:
        print(f"❌ [Data Fetch Error]: {e}")
        market_full, account_data, recent_summaries = {}, {}, []
    
    # 2. 计算当前时间、星期几 (UTC+8)
    tz_cn = pytz.timezone('Asia/Shanghai')
    now = datetime.now(tz_cn)
    weekday_str = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    system_time_str = f"{now.strftime('%Y-%m-%d %H:%M')} {weekday_str} (UTC+8)"

    leverage = int(os.getenv('LEVERAGE', 10))
    balance = account_data.get('balance', 0)
    if balance < 10: balance = 10000 
    
    # 3. 动态获取价格 (防止策略模式下 15m 不存在报错)
    analysis_dict = market_full.get("analysis", {})
    # 优先找小周期，如果没有(策略模式)，就找存在的最小周期
    target_tf = '15m' if '15m' in analysis_dict else (list(analysis_dict.keys())[0] if analysis_dict else None)
    
    if target_tf and target_tf in analysis_dict:
        analysis_data = analysis_dict[target_tf]
        current_price = analysis_data.get("price", 0)
        # ATR 依然尝试取 15m，如果没有则用当前价格估算
        atr_15m = analysis_dict.get('15m', {}).get('atr', current_price * 0.01)
    else:
        current_price = 0
        atr_15m = 0
    
    # 简化市场数据构建
    indicators_summary = {}
    # 这里只保留 market_full 里实际存在的周期
    for tf, tf_data in analysis_dict.items():
        if tf_data:
            vp_data = tf_data.get("vp", {})
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "rsi": tf_data.get("rsi"),
                "atr": tf_data.get("atr"),
                "ema": tf_data.get("ema"),
                "vp": {"poc": vp_data.get("poc"), "vah": vp_data.get("vah"), "val": vp_data.get("val"), "hvns": vp_data.get("hvns", [])}
            }

    market_context_llm = {
        "current_price": current_price,
        "atr_15m": atr_15m,
        "sentiment": market_full.get("sentiment"),
        "technical_indicators": indicators_summary 
    }
    
    formatted_market_data = format_market_data_to_markdown(market_context_llm)
    
    # 历史记录
    history_entries = []
    for s in recent_summaries:
        ts = s.get('timestamp', 'Unknown')
        entry = f"⏰ [{ts}] View: {s.get('content', '')}\n🧠 Logic: {s.get('strategy_logic', '')}"
        history_entries.append(entry)
    formatted_history_text = "\n\n".join(history_entries) if history_entries else "(暂无历史记录)"

    # 选择 Prompt
    if is_real_exec:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [{"id": o['order_id'], "side": o['side'], "type": o['type'], "price": o['price'], "amount": o['amount']} for o in raw_orders]
        
        system_prompt = REAL_TRADE_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            system_time=system_time_str,  # 传入当前时间
            leverage=leverage,
            current_price=current_price,
            atr_15m=atr_15m,
            balance=balance,
            positions_json=json.dumps(account_data.get('real_positions', []), ensure_ascii=False),
            orders_json=json.dumps(display_orders, ensure_ascii=False),
            formatted_market_data=formatted_market_data,
            history_text=formatted_history_text,
        )
    else:
        raw_mock_orders = account_data.get('mock_open_orders', [])
        display_mock_orders = [{"id": o['order_id'], "side": o['side'], "price": o['price'], "tp": o['take_profit'], "sl": o['stop_loss']} for o in raw_mock_orders]

        system_prompt = STRATEGY_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            system_time=system_time_str, # 传入当前时间
            current_price=current_price,
            atr_15m=atr_15m,
            positions_json=json.dumps(account_data.get('real_positions', []), ensure_ascii=False),
            orders_json=json.dumps(display_mock_orders, ensure_ascii=False),
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
    print(f"--- [Node] Agent: {config.get('model')} is thinking for {symbol} ---")
    
    try:
        current_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5) 
        ).with_structured_output(AgentOutput)
        
        response = current_llm.invoke(state['messages'])
        return {**state, "final_output": response.model_dump()}
    except Exception as e:
        print(f"❌ [LLM Error]: {e}")
        return {**state, "final_output": {"summary": {"current_trend":"Error","key_levels":"","strategy_thought":str(e),"predict":""},"orders":[]}}

def execution_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    agent_name = config.get('model', 'Unknown')
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    print(f"--- [Node] Execution: {symbol} | Mode: {trade_mode} ---")
    
    output = state['final_output']
    if not output: return state

    summary = output.get('summary', {})
    orders = output.get('orders', [])
    
    # 保存分析
    try:
        database.save_summary(symbol, agent_name, f"[{trade_mode}] {summary.get('predict')}", summary.get('strategy_thought'))
    except Exception as e: print(f"⚠️ DB Error: {e}")

    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': continue
        
        log_reason = order.get('reason', '')
        
        if trade_mode == 'REAL':
            # === 实盘执行模式 ===
            
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    print(f"🔄 [REAL] Cancel Order: {cancel_id}")
                    market_tool.place_real_order(symbol, 'CANCEL', order)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, "实盘撤单", "REAL")

            # 2. 平仓
            elif action == 'CLOSE':
                print(f"🎯 [REAL] Close Position")
                market_tool.place_real_order(symbol, 'CLOSE', order)
                database.save_order_log("CLOSE", symbol, agent_name, "CLOSE", 0, 0, 0, log_reason, "REAL")

            # 3. 开仓 / 加仓 / 减仓 (TP/SL)
            elif action in ['BUY_LIMIT', 'SELL_LIMIT', 'ADD_TP', 'ADD_SL']:
                price = order.get('entry_price') # 对于 TP/SL，entry_price 即触发价
                
                # Side 记录日志用
                side_log = action
                if action == 'BUY_LIMIT': side_log = 'BUY'
                elif action == 'SELL_LIMIT': side_log = 'SELL'
                
                print(f"🚀 [REAL] Action: {action} @ {price}")
                
                # 调用 MarketTool
                res = market_tool.place_real_order(symbol, action, order)
                
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, side_log, 
                                            price, 0, 0, log_reason, "REAL")

        else:
            # === 策略模拟模式 ===
            if action == 'CANCEL':
                if cid := order.get('cancel_order_id'):
                    database.cancel_mock_order(cid)
                    database.save_order_log(cid, symbol, agent_name, "CANCEL", 0, 0, 0, "策略撤单", "STRATEGY")

            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                side = 'BUY' if 'BUY' in action else 'SELL'
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                
                print(f"💡 [STRATEGY 4H+] Plan: {side} @ {order.get('entry_price')} | TP: {order.get('take_profit')} | SL: {order.get('stop_loss')}")
                
                database.create_mock_order(symbol, side, order['entry_price'], order['amount'], order['stop_loss'], order['take_profit'])
                database.save_order_log(mock_id, symbol, agent_name, side, order.get('entry_price'), order.get('take_profit'), order.get('stop_loss'), f"[Strategy] {log_reason}", "STRATEGY")

    return state

# ==========================================
# 5. Graph 编译与运行
# ==========================================

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
    print(f"\n🚀 Launching Agent: {symbol} | Mode: {config.get('mode', 'STRATEGY')}")
    try:
        app.invoke({
            "symbol": symbol, "messages": [], "agent_config": config,
            "market_context": {}, "account_context": {}, "history_context": [], "final_output": {}
        })
    except Exception as e:
        print(f"❌ Graph Error: {e}")
        import traceback
        traceback.print_exc()