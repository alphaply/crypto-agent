from typing import Annotated, List, TypedDict, Union
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
import json
import os
import database
from market_data import MarketTool

# 1. 定义输出结构
class OrderParams(BaseModel):
    """交易指令"""
    action: str = Field(description="动作: 'BUY_LIMIT', 'SELL_LIMIT', 'CANCEL', 'CLOSE', 'NO_ACTION'")
    cancel_order_id: str = Field(description="如果要撤单，填入对应的 order_id，否则留空", default="")
    entry_price: float = Field(description="入场价格")
    amount: float = Field(description="开仓数量 (单位: 币的个数)")
    take_profit: float = Field(description="止盈价格", default=0.0)
    stop_loss: float = Field(description="止损价格", default=0.0)
    reason: str = Field(description="操作理由")

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    current_trend: str = Field(description="当前趋势 (Bullish/Bearish/Range)")
    key_levels: str = Field(description="关键点位")
    strategy_thought: str = Field(description="思考过程")

class AgentOutput(BaseModel):
    summary: MarketSummaryParams
    orders: List[OrderParams]

# 2. 定义状态
class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    market_context: dict
    account_context: dict
    history_context: List[dict]
    final_output: dict

# 初始化工具
market_tool = MarketTool()
llm = ChatOpenAI(model="qwen3-max-preview", temperature=0.5).with_structured_output(AgentOutput)

# 全局配置
TRADING_MODE = os.getenv('TRADING_MODE', 'MOCK')
LEVERAGE = int(os.getenv('LEVERAGE', 10))
RISK_PER_TRADE_PCT = float(os.getenv('RISK_PER_TRADE_PCT', 0.05))
# 从环境变量读取白名单，如果没有则使用默认值
REAL_TRADE_WHITELIST = os.getenv('REAL_TRADE_WHITELIST', "ETH/USDT,SOL/USDT").split(',')

def start_node(state: AgentState):
    symbol = state['symbol']
    print(f"\n--- [Node] Start: Analyzing {symbol} ({TRADING_MODE} Mode) ---")
    
    # 获取基础数据
    market_full = market_tool.get_market_analysis(symbol)
    account_data = market_tool.get_account_status(symbol)
    recent_summaries = database.get_recent_summaries(symbol, limit=3)
    
    # 资金管理逻辑
    balance = account_data.get('balance', 0)
    trade_size_usdt = balance * RISK_PER_TRADE_PCT * LEVERAGE 
    if TRADING_MODE == 'MOCK':
        balance = 10000
        trade_size_usdt = 1000 

    # 【核心逻辑】根据白名单过滤给 AI 看的订单信息
    if symbol in REAL_TRADE_WHITELIST:
        raw_orders = account_data.get('real_open_orders', [])
        display_orders = []
        for o in raw_orders:
            # 格式化输出，让 AI 明白 LIMIT 是入场，STOP/TAKE 是保护
            o_type = o.get('type', 'UNKNOWN')
            o_side = o.get('side', 'UNKNOWN')
            o_amt = o.get('amount', 0)
            # 条件单可能没有 price，只有 stop_price
            o_price = o.get('price') if o.get('price') and o.get('price') > 0 else o.get('stop_price', 0)
            
            display_orders.append({
                "order_id": o.get('order_id'),
                "type": o_type,
                "side": o_side,
                "amount": o_amt,
                "price_or_trigger": o_price,
                "label": "ENTRY_ORDER" if o_type == 'LIMIT' else "PROTECTION_ORDER"
            })
        order_type_label = "实盘活跃订单 (Real Orders - 包含限价与止盈止损)"
    else:
        display_orders = account_data.get('mock_open_orders', [])
        order_type_label = "模拟挂单 (Mock Orders)"

    # 数据清洗
    market_context_llm = {
        "symbol": symbol,
        "sentiment": market_full.get("sentiment"),
        "analysis": {tf: {k: v for k, v in data.items() if k != "df_raw"} 
                     for tf, data in market_full.get("analysis", {}).items() if data}
    }

    history_text = "\n".join([f"[{s['timestamp']}] {s['content']}" for s in recent_summaries])

    return {
        "market_context": market_full,
        "account_context": account_data,
        "history_context": recent_summaries,
        "messages": [SystemMessage(content=f"""
你是专业的加密货币量化交易 Agent。你正在分析 **{symbol}**。
做单尽量做短中线的，只做信心分数高的。你每 15 分钟检查一次。

【交易模式】: **{TRADING_MODE}**
【资金管理】: 
- 权益余额: {balance:.2f} USDT | 杠杆: {LEVERAGE}x
- 建议单笔下单价值: {trade_size_usdt:.2f} USDT
- 注意：输出 amount 时请计算币数 (例如: {trade_size_usdt} / EntryPrice)。

【当前持仓 (Positions)】:
{json.dumps(account_data['real_positions'], ensure_ascii=False)}

【{order_type_label}】: 
{json.dumps(display_orders, ensure_ascii=False)}

【规则与任务】:
1. **查重原则**：如果【{order_type_label}】中已有同方向的 LIMIT 订单，除非当前价格大幅偏离你的理想位，否则严禁再次下单！
2. **保护原则**：所有 LIMIT 入场单必须配为止损 (STOP_MARKET)。
3. **撤单逻辑**：如果发现旧订单的逻辑已失效，请执行 'CANCEL' 并填入对应的 order_id。
4. **Volume Profile 提示**：POC 是核心支撑/阻力；VAH/VAL 是区间边界；LVN 区域价格易加速。其他指标你都懂的

【全量市场数据】:
{json.dumps(market_context_llm, ensure_ascii=False)}

【历史回顾】:
{history_text}
        """)]
    }

def agent_node(state: AgentState):
    print(f"--- [Node] Agent: Thinking {state['symbol']} ---")
    response = llm.invoke(state['messages'])
    return {"final_output": response.dict()}

def execution_node(state: AgentState):
    symbol = state['symbol']
    print(f"--- [Node] Execution: Processing {symbol} ---")
    output = state['final_output']
    summary = output['summary']
    orders = output['orders']
    
    # 1. 保存行情分析
    content = f"Trend: {summary['current_trend']}\nLevels: {summary['key_levels']}"
    database.save_summary(symbol, content, summary['strategy_thought'])

    # 2. 遍历执行指令
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': 
            continue
            
        # --- A. 撤单逻辑 ---
        if action == 'CANCEL':
            cancel_id = order.get('cancel_order_id')
            if cancel_id:
                reason_text = f"撤销单据: {cancel_id}"
                database.cancel_mock_order(cancel_id) # 内部会同时更新 orders 日志表状态
                database.save_order_log(symbol, "CANCEL", 0, 0, 0, reason_text)
                
                if TRADING_MODE == 'REAL' and symbol in REAL_TRADE_WHITELIST:
                    market_tool.place_real_order(symbol, 'CANCEL', order)
        elif action == 'CLOSE':
            print(f"🎯 [Action] 尝试平掉 {symbol} 现有持仓")
            if TRADING_MODE == 'REAL' and symbol in REAL_TRADE_WHITELIST:
                market_tool.place_real_order(symbol, 'CLOSE', order)
            # 模拟模式下可以清空模拟数据库相关记录
            database.save_order_log(symbol, "CLOSE", order['entry_price'], 0, 0, order['reason'])
        

        # --- B. 下单逻辑 ---
        elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
            # 【重要】实盘查重预防：防止 AI 在已有挂单时疯狂重复下单
            if TRADING_MODE == 'REAL' and symbol in REAL_TRADE_WHITELIST:
                existing_real = state['account_context'].get('real_open_orders', [])
                side_to_check = 'buy' if 'BUY' in action else 'sell'
                # 检查是否有同方向的 LIMIT 挂单
                has_existing = any(o for o in existing_real if o['side'].lower() == side_to_check and o['type'] == 'LIMIT')
                
                if has_existing:
                    print(f"⚠️ [Skip] {symbol} 实盘已有 {side_to_check} 挂单，防止重复执行。")
                    continue

            # 正常执行下单流程
            side = 'buy' if 'BUY' in action else 'sell'
            
            # 记录到本地数据库
            new_id = database.create_mock_order(
                symbol, side, order['entry_price'], order['amount'], 
                order['stop_loss'], order['take_profit']
            )
            database.save_order_log(
                symbol, side, order['entry_price'], order['take_profit'], 
                order['stop_loss'], order['reason']
            )
            
            print(f"✅ [Log] Created Order {new_id} for {symbol}")

            # 实盘执行
            if TRADING_MODE == 'REAL' and symbol in REAL_TRADE_WHITELIST:
                print(f"🚀 [REAL TRADE] Executing {action} for {symbol}")
                market_tool.place_real_order(symbol, action, order)

    return state

# --- Graph 构建 ---
workflow = StateGraph(AgentState)
workflow.add_node("start", start_node)
workflow.add_node("agent", agent_node)
workflow.add_node("execution", execution_node)

workflow.set_entry_point("start")
workflow.add_edge("start", "agent")
workflow.add_edge("agent", "execution")
workflow.add_edge("execution", END)

app = workflow.compile()

def run_agent_for_symbol(symbol):
    """主程序调用的入口"""
    initial_state = {
        "symbol": symbol,
        "messages": []
    }
    try:
        app.invoke(initial_state)
    except Exception as e:
        print(f"❌ Graph Error for {symbol}: {e}")