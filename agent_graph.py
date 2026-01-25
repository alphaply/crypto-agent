import json
import os
import time
from typing import Annotated, List, TypedDict, Union
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from datetime import datetime
import time


# 引入自定义模块
import database
from market_data import MarketTool

# 加载环境变量
load_dotenv()

# ==========================================
# 1. 配置加载与工具初始化
# ==========================================

market_tool = MarketTool()

# ==========================================
# 2. 定义 Pydantic 输出结构
# ==========================================

class OrderParams(BaseModel):
    """交易指令结构"""
    action: str = Field(
        description="动作: 'BUY_LIMIT' (做多), 'SELL_LIMIT' (做空), 'CANCEL' (撤单), 'CLOSE' (平仓), 'NO_ACTION' (观望)",
        pattern="^(BUY_LIMIT|SELL_LIMIT|CANCEL|CLOSE|NO_ACTION)$"
    )
    cancel_order_id: str = Field(description="撤单时填入 ID，否则留空", default="")
    entry_price: float = Field(description="挂单价格")
    amount: float = Field(description="下单数量 (币的个数)")
    take_profit: float = Field(description="止盈价格", default=0.0)
    stop_loss: float = Field(description="止损价格", default=0.0)
    reason: str = Field(description="简短的决策理由")

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    current_trend: str = Field(description="趋势判断 (Bullish/Bearish/Range/Volatile)")
    key_levels: str = Field(description="关键支撑与阻力位")
    strategy_thought: str = Field(description="详细的思维链分析")

class AgentOutput(BaseModel):
    summary: MarketSummaryParams
    orders: List[OrderParams]

# ==========================================
# 3. 定义 State 状态
# ==========================================

class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    agent_config: dict       # 存储当前币种的 LLM 配置
    market_context: dict
    account_context: dict
    history_context: List[dict]
    final_output: dict

# ==========================================
# 4. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState):
    symbol = state['symbol']
    
    # 1. 获取当前币种的配置
    config = state['agent_config']
    is_real_trade = config.get('real_trade', False)
    mode_str = "REAL" if is_real_trade else "MOCK"
    
    print(f"\n--- [Node] Start: Analyzing {symbol} using {config.get('model')} ({mode_str} Mode) ---")

    # 2. 获取数据
    market_full = market_tool.get_market_analysis(symbol)
    account_data = market_tool.get_account_status(symbol)
    recent_summaries = database.get_recent_summaries(symbol, limit=10) # 获取最近 10 条
    
    # 3. 资金管理 (读取全局杠杆配置，或从 config 读取)
    leverage = int(os.getenv('LEVERAGE', 10))
    risk_pct = float(os.getenv('RISK_PER_TRADE_PCT', 0.05))
    balance = account_data.get('balance', 0)
    
    # 模拟资金覆盖
    if not is_real_trade:
        balance = 10000 
        
    trade_size_usdt = balance * risk_pct * leverage

    # 4. 订单数据过滤 (根据是否实盘展示不同数据)
    if is_real_trade:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = []
        for o in raw_orders:
            o_type = o.get('type', 'UNKNOWN')
            o_price = o.get('price') if o.get('price') and o.get('price') > 0 else o.get('stop_price', 0)
            display_orders.append({
                "id": o.get('order_id'),
                "side": o.get('side'),
                "type": o_type,
                "price": o_price,
                "amount": o.get('amount'),
                "desc": "ENTRY" if o_type == 'LIMIT' else "TP/SL Protection"
            })
        orders_context_str = f"【实盘活跃订单 (Real Orders)】:\n{json.dumps(display_orders, ensure_ascii=False)}"
    else:
        display_orders = account_data.get('mock_open_orders', [])
        orders_context_str = f"【模拟挂单 (Mock Orders)】:\n{json.dumps(display_orders, ensure_ascii=False)}"

    # 5. 构建 Prompt
    market_context_llm = {
        "price": market_full.get("analysis", {}).get("15m", {}).get("price"),
        "sentiment": market_full.get("sentiment"),
        "analysis_summary": {tf: data.get("vp", {}) for tf, data in market_full.get("analysis", {}).items() if data}
    }
    
    # 历史记录字符串拼接
    history_text = "\n".join([
        f"[{s['timestamp']}] Agent: {s.get('agent_name', 'Unknown')}\nLogic: {s['strategy_logic'][:512]}..." 
        for s in recent_summaries
    ])
    now = datetime.now()
    weekdays_zh = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = weekdays_zh[now.weekday()]
    # 计算时区 (例如 UTC+8)
    tz_offset = -time.timezone if (time.localtime().tm_isdst == 0) else -time.altzone
    tz_offset_hours = int(tz_offset / 3600)
    tz_str = f"UTC{'+' if tz_offset_hours >= 0 else ''}{tz_offset_hours}"

    # 组合成完整的时间字符串，例如: "2026-01-25 11:00:00 (星期日) UTC+8"
    full_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({weekday_str}) {tz_str}"
    system_prompt = f"""
你是由 {config.get('model')} 驱动的专业加密货币量化交易 Agent。
当前正在监控: **{symbol}** | 当前时间: {full_time_str}
交易模式: **{mode_str} (实盘: {is_real_trade})**

【核心策略：日内波段 (Intraday Swing)】
1. **不做噪音交易**：你每 15 分钟运行一次。不要被微小波动干扰。你的目标是捕捉 1h-4h 级别的趋势单。
2. **高胜率入场**：只有当信心分数极高时才开仓。根据指标进行挂单操作。
3. 交易每次下单只能固定的仓位（轻仓）
4. **风控第一**：所有 BUY_LIMIT/SELL_LIMIT 必须带上 止盈止损。

【资金状态】
- 可用余额: {balance:.2f} USDT
- 建议单笔名义价值: {trade_size_usdt:.2f} USDT (请自行换算成 coin amount)

【当前持仓】
{json.dumps(account_data['real_positions'], ensure_ascii=False)}

{orders_context_str}

【市场概况】
{json.dumps(market_context_llm, ensure_ascii=False)}

【近期思路回顾】
{history_text}

请严格按格式输出决策(中文)。如果没有明确机会，action 选 "NO_ACTION"。
"""

    return {
        "symbol": symbol,
        "agent_config": config,
        "market_context": market_full,
        "account_context": account_data,
        "history_context": recent_summaries,
        "messages": [SystemMessage(content=system_prompt)]
    }

def agent_node(state: AgentState):
    config = state['agent_config']
    symbol = state['symbol']
    print(f"--- [Node] Agent: {config.get('model')} is thinking for {symbol} ---")
    
    # 动态初始化 LLM
    try:
        current_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5)
        ).with_structured_output(AgentOutput)
        
        response = current_llm.invoke(state['messages'])
        return {"final_output": response.dict()}
        
    except Exception as e:
        print(f"❌ LLM 调用失败 ({symbol}): {e}")
        # 返回空结果防止 crash
        return {"final_output": {"summary": {"current_trend": "Error", "key_levels": "", "strategy_thought": str(e)}, "orders": []}}

def execution_node(state: AgentState):
    symbol = state['symbol']
    config = state['agent_config']
    is_real_trade = config.get('real_trade', False)
    
    print(f"--- [Node] Execution: Processing {symbol} ---")
    
    output = state['final_output']
    summary = output.get('summary', {})
    orders = output.get('orders', [])
    
    # 1. 保存总结到数据库 (增加 agent_name)
    # 假设 database.save_summary 已更新为 def save_summary(symbol, agent_name, content, strategy_logic):
    content = f"Trend: {summary.get('current_trend')}\nLevels: {summary.get('key_levels')}"
    try:
        # 如果你的 save_summary 还没改，请修改 database.py 或这里适配
        database.save_summary(symbol, config.get('model'), content, summary.get('strategy_thought'))
    except TypeError:
        # 兼容旧接口
        database.save_summary(symbol, content, summary.get('strategy_thought'))

    # 2. 执行订单逻辑
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': 
            continue
            
        # --- A. 撤单 ---
        if action == 'CANCEL':
            cancel_id = order.get('cancel_order_id')
            if cancel_id:
                if cancel_id == "ALL":
                    # 简化逻辑：如果是 ALL，这里需要额外处理，暂时只处理单 ID
                    pass 
                else:
                    database.cancel_mock_order(cancel_id)
                    database.save_order_log(cancel_id, symbol, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}")
                    
                    if is_real_trade:
                        market_tool.place_real_order(symbol, 'CANCEL', order)

        # --- B. 平仓 ---
        elif action == 'CLOSE':
            print(f"🎯 [Action] 平仓指令: {symbol}")
            if is_real_trade:
                market_tool.place_real_order(symbol, 'CLOSE', order)
            database.save_order_log("CLOSE_CMD", symbol, "CLOSE", order['entry_price'], 0, 0, order['reason'])

        # --- C. 开仓 (LIMIT) ---
        elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
            # 1. 模拟盘落库
            side = 'buy' if 'BUY' in action else 'sell'
            new_id = database.create_mock_order(
                symbol, side, 
                order['entry_price'], 
                order['amount'], 
                order['stop_loss'], 
                order['take_profit']
            )
            agent_name = config.get('model', 'Unknown')
            database.save_order_log(new_id, symbol,agent_name, side, order['entry_price'], order['take_profit'], order['stop_loss'], order['reason'])
            print(f"✅ [Mock DB] 挂单已记录: {symbol} {side} @ {order['entry_price']}")

            # 2. 实盘执行
            if is_real_trade:
                # 再次执行双重查重（防止 LLM 幻觉导致忽略查重指令）
                existing = state['account_context'].get('real_open_orders', [])
                has_duplicate = any(o for o in existing if o['side'].lower() == side and o['type'] == 'LIMIT')
                
                if has_duplicate:
                    print(f"⚠️ [Risk Control] 实盘已有 {side} 单，拦截重复下单。")
                else:
                    print(f"🚀 [REAL TRADE] 发送交易所: {symbol} {action}")
                    market_tool.place_real_order(symbol, action, order)

    return state

# ==========================================
# 5. Graph 编译
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
    接收具体的配置对象运行 Agent
    """
    symbol = config['symbol']
    is_real_trade = config.get('real_trade', False)
    mode_str = "REAL" if is_real_trade else "MOCK"
    
    # 打印时带上模型名，方便区分
    print(f"\n--- [Node] Start: {symbol} using {config.get('model')} ({mode_str}) ---")

    # 1. 初始化 State (直接使用传入的 config)
    initial_state = {
        "symbol": symbol,
        "messages": [],
        "agent_config": config,  # <--- 重点：直接使用传入的配置
        "market_context": {},
        "account_context": {},
        "history_context": [],
        "final_output": {}
    }

    # 2. 运行 Graph
    try:
        app.invoke(initial_state)
    except Exception as e:
        print(f"❌ Graph Error for {symbol} ({config.get('model')}): {e}")