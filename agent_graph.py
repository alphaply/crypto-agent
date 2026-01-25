import json
import os
import time
import math
import uuid  # ✅ 新增: 用于生成策略单的模拟ID
from typing import Annotated, List, TypedDict, Union, Dict, Any, Optional
from datetime import datetime

# LangChain / LangGraph Imports
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# 自定义模块 (需确保这些文件存在)
import database
from market_data import MarketTool

# 加载环境变量
load_dotenv()
market_tool = MarketTool()

# ==========================================
# 0. Prompt 模板定义 (根据模式区分)
# ==========================================

# A. 实盘执行模式 Prompt (无 TP/SL，专注挂单)
REAL_TRADE_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **专业实盘交易执行员 (Execution Trader)**。
当前监控: **{symbol}** | 模式: 🔴 实盘交易 (REAL EXECUTION) | 杠杆: {leverage}x
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
你的职责不是预测长远未来，而是寻找**当前时刻**高胜率的短线挂单点位，或者管理现有仓位。
**实盘模式下，你不需要设置止盈止损 (TP/SL)，专注于优异的进场位置。**

【权限与指令】
1. **BUY_LIMIT**: 挂单接多 (价格必须 < 现价)。
2. **SELL_LIMIT**: 挂单做空 (价格必须 > 现价)。
3. **CLOSE**: 市价平掉当前持仓。
4. **CANCEL**: 撤销指定的挂单。
5. **NO_ACTION**: 没有极高把握时，保持空仓。

【决策铁律】
1. **点位精准**: 参考 HVN (筹码峰) 和 VAL/VAH。不要在半山腰挂单。
2. **防滑点**: 严禁使用市价开仓，必须使用 Limit 单。
3. **趋势顺势**: EMA 多头排列时尽量不做空，反之亦然，除非乖离率极大约束。

【资金与持仓】
可用余额: {balance:.2f} USDT
现有持仓: {positions_json}
活跃挂单: {orders_json}

【全量市场数据】
{formatted_market_data}

【输出要求】
请输出 JSON，包含 `orders` 列表。
- `action`: BUY_LIMIT / SELL_LIMIT / CLOSE / CANCEL / NO_ACTION
- `entry_price`: 挂单价格
- `amount`: 下单数量 (币的个数)
- `reason`: 简短的执行理由 (例如："回踩 15m HVN 接多")
- `take_profit`: 填 0
- `stop_loss`: 填 0
"""

# B. 策略分析模式 Prompt (需 TP/SL，专注趋势)
STRATEGY_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **资深加密货币策略分析师 (Crypto Strategist)**。
当前监控: **{symbol}** | 模式: 🔵 策略分析 (STRATEGY IDEA)
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
你需要分析中长线趋势，生成具有高盈亏比 (R/R Ratio) 的交易计划。
**策略模式下，必须明确给出 止损(SL) 和 止盈(TP) 点位。**

【策略要求】
1. **盈亏比**: 预期 R/R 必须 > 2.0。
2. **逻辑支撑**: 必须基于结构位 (Structure)、供需区 (Supply/Demand) 或流动性 (Liquidity) 制定计划。
3. **完整性**: 必须包含入场价、止损价、止盈价。

【全量市场数据】
{formatted_market_data}

【输出要求】
请输出 JSON。
- `action`: BUY_LIMIT / SELL_LIMIT / NO_ACTION
- `entry_price`: 建议入场价
- `take_profit`: 建议止盈价 (必填)
- `stop_loss`: 建议止损价 (必填)
- `reason`: 详细的策略逻辑，包含 R/R 计算。
"""

# ==========================================
# 1. 定义 Pydantic 输出结构 (Schema)
# ==========================================

class OrderParams(BaseModel):
    """交易指令结构"""
    reason: str = Field(description="简短的决策理由")
    action: str = Field(
        description="动作: 'BUY_LIMIT', 'SELL_LIMIT', 'CANCEL', 'CLOSE', 'NO_ACTION'",
        pattern="^(BUY_LIMIT|SELL_LIMIT|CANCEL|CLOSE|NO_ACTION)$"
    )
    cancel_order_id: str = Field(description="撤单时填入 ID，否则留空", default="")
    entry_price: float = Field(description="挂单价格")
    amount: float = Field(description="下单数量 (币的个数，非 USDT 金额)", default=0.0)
    take_profit: float = Field(description="止盈价格", default=0.0)
    stop_loss: float = Field(description="止损价格", default=0.0)

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    current_trend: str = Field(description="趋势判断")
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
    """
    # --- 辅助函数：动态价格格式化 ---
    def fmt_price(price):
        if price is None or price == 0: return "0"
        abs_p = abs(price)
        if abs_p >= 1000: return f"{int(price)}"      
        if abs_p >= 1: return f"{price:.2f}"          
        if abs_p >= 0.01: return f"{price:.4f}"       
        return f"{price:.8f}".rstrip('0')             

    # --- 辅助函数：大数字格式化 ---
    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    # 1. 提取基础信息
    current_price = data.get("current_price", 0)
    atr_15m = data.get("atr_15m", 0)
    
    # 2. 格式化情绪/衍生品数据
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    oi = sent.get("open_interest", 0)
    
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    oi_str = fmt_num(oi)
    
    header = (
        f"**Snapshot** | Price: {fmt_price(current_price)} | 15m ATR: {fmt_price(atr_15m)}\n"
        f"Sentiment: Fund: {funding:.4f}% | OI: {oi_str} | Vol24h: {vol_24h}\n"
    )

    # 3. 构建多周期技术指标表格
    table_header = (
        "| TF | RSI | EMA (20/50/100/200) | POC | VA Range | HVN (Chips Peaks) |\n"
        "|---|---|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    timeframes = ['5m', '15m', '1h', '4h', '1d']
    
    for tf in timeframes:
        if tf not in indicators: continue
        d = indicators[tf]
        
        rsi = f"{d.get('rsi', 0):.1f}"
        ema = d.get('ema', {})
        e20 = fmt_price(ema.get('ema_20', 0))
        e50 = fmt_price(ema.get('ema_50', 0))
        e100 = fmt_price(ema.get('ema_100', 0))
        e200 = fmt_price(ema.get('ema_200', 0))
        ema_str = f"{e20}/{e50}/{e100}/{e200}"
        
        vp = d.get('vp', {})
        poc = fmt_price(vp.get('poc', 0))
        val = fmt_price(vp.get('val', 0))
        vah = fmt_price(vp.get('vah', 0))
        va_range = f"{val}-{vah}"
        
        raw_hvns = vp.get('hvns', [])
        top_hvns = sorted(raw_hvns, reverse=True)[:3]
        hvn_str = ",".join([fmt_price(h) for h in top_hvns])
        
        row = f"| {tf} | {rsi} | {ema_str} | {poc} | {va_range} | {hvn_str} |"
        rows.append(row)
    
    return header + table_header + "\n".join(rows)

# ==========================================
# 4. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    
    # ✅ 1. 核心修改：获取模式 (STRATEGY / REAL)
    # config['mode'] 应该由 main_scheduler 传入
    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    
    print(f"\n--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        # 2. 获取数据 (无论什么模式，都需要全量数据)
        market_full = market_tool.get_market_analysis(symbol)
        # 获取账户数据 (实盘模式读交易所，策略模式读数据库或模拟余额)
        # 注意：这里我们统一传入 is_real=is_real_exec，以便实盘模式能拿到真实持仓
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec)
        recent_summaries = database.get_recent_summaries(symbol, limit=10)
    except Exception as e:
        print(f"❌ [Data Fetch Error]: {e}")
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        recent_summaries = []
    
    # 3. 计算资金
    leverage = int(os.getenv('LEVERAGE', 10))
    # risk_pct = float(os.getenv('RISK_PER_TRADE_PCT', 0.05))
    balance = account_data.get('balance', 0)
    
    # 兜底资金逻辑
    if balance < 10: balance = 10000 
    
    # 4. 构建市场数据上下文 (通用)
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    # ATR 容错
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0
    
    indicators_summary = {}
    for tf in ['5m', '15m', '1h', '4h', '1d']:
        tf_data = market_full.get("analysis", {}).get(tf)
        if tf_data:
            vp_data = tf_data.get("vp", {})
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "recent_closes": tf_data.get("recent_closes", [])[-5:],
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
    
    # 5. ✅ 核心修改：根据模式选择 Prompt
    if is_real_exec:
        # --- 实盘模式 Prompt ---
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = [{
            "id": o.get('order_id'), "side": o.get('side'), "type": o.get('type'), 
            "price": o.get('price'), "amount": o.get('amount')
        } for o in raw_orders]
        
        system_prompt = REAL_TRADE_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            leverage=leverage,
            current_price=market_context_llm['current_price'],
            atr_15m=market_context_llm['atr_15m'],
            balance=balance,
            positions_json=json.dumps(account_data.get('real_positions', []), ensure_ascii=False),
            orders_json=json.dumps(display_orders, ensure_ascii=False),
            formatted_market_data=formatted_market_data
        )
    else:
        # --- 策略模式 Prompt ---
        system_prompt = STRATEGY_PROMPT_TEMPLATE.format(
            model=config.get('model'),
            symbol=symbol,
            current_price=market_context_llm['current_price'],
            atr_15m=market_context_llm['atr_15m'],
            formatted_market_data=formatted_market_data
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
        print(f"❌ [LLM Error] ({symbol}): {e}")
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
    
    # ✅ 获取当前模式
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    print(f"--- [Node] Execution: {symbol} | Mode: {trade_mode} ---")
    
    output = state['final_output']
    if not output: return state

    summary = output.get('summary', {})
    orders = output.get('orders', [])
    
    # 1. 保存分析日志 (两种模式都保存)
    content = f"[{trade_mode}] Trend: {summary.get('current_trend')}\nPredict: {summary.get('predict')}"
    try:
        database.save_summary(symbol, agent_name, content, summary.get('strategy_thought'))
    except Exception as db_err:
        print(f"⚠️ [DB Error] Save summary failed: {db_err}")

    # 2. 执行/记录订单
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': continue
        
        log_reason = order.get('reason', '')

        # ==========================================
        # 分支 A: 实盘执行 (REAL)
        # ==========================================
        if trade_mode == 'REAL':
            # 强制清空 TP/SL (防止幻觉)
            order['take_profit'] = 0
            order['stop_loss'] = 0
            
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    print(f"🔄 [REAL] Cancel: {cancel_id}")
                    market_tool.place_real_order(symbol, 'CANCEL', order)
                    # 记录日志
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}", trade_mode="REAL")

            # 2. 平仓
            elif action == 'CLOSE':
                print(f"🎯 [REAL] Close Position")
                market_tool.place_real_order(symbol, 'CLOSE', order)
                database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", 0, 0, 0, log_reason, trade_mode="REAL")

            # 3. 开仓 (Limit)
            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                # 简单防重 (仅演示，建议放在 market_tool)
                existing = state['account_context'].get('real_open_orders', [])
                price = order.get('entry_price')
                side = 'buy' if 'BUY' in action else 'sell'
                
                # ... 防重逻辑略 ...

                print(f"🚀 [REAL] Order: {action} @ {price}")
                res = market_tool.place_real_order(symbol, action, order)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, side, 
                                            price, 0, 0, log_reason, trade_mode="REAL")

        # ==========================================
        # 分支 B: 策略模式 (STRATEGY)
        # ==========================================
        else:
            # 仅记录，不操作 API
            side = 'BUY' if 'BUY' in action else 'SELL'
            if action == 'NO_ACTION': continue

            # 生成模拟 ID
            mock_id = f"ST-{uuid.uuid4().hex[:6]}"
            
            print(f"💡 [STRATEGY] Idea: {side} @ {order.get('entry_price')} | TP: {order.get('take_profit')} | SL: {order.get('stop_loss')}")
            
            database.save_order_log(
                mock_id, symbol, agent_name, side, 
                order.get('entry_price'), 
                order.get('take_profit'), 
                order.get('stop_loss'), 
                f"[Strategy] {log_reason}",
                trade_mode="STRATEGY"
            )

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
    """
    运行 Agent 的主入口函数
    """
    symbol = config['symbol']
    
    # ✅ 获取模式，优先使用 config['mode']，如果没有则默认为 STRATEGY
    # 可以在 .env 或 dashboard 调用时控制这个字段
    mode_str = config.get('mode', 'STRATEGY').upper()
    
    print(f"\n========================================================")
    print(f"🚀 Launching Agent: {symbol} | Model: {config.get('model')} | Mode: {mode_str}")
    print(f"========================================================")

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
        print(f"❌ Critical Graph Error for {symbol}: {e}")
        import traceback
        traceback.print_exc()