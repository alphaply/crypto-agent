from typing import Annotated, List, TypedDict, Union
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
import json
import database
from market_data import MarketTool
from langchain_core.messages import BaseMessage
import pandas as pd

# 1. 定义输出结构 (增加撤单功能)
class OrderParams(BaseModel):
    """交易指令"""
    action: str = Field(description="动作: 'BUY_LIMIT', 'SELL_LIMIT', 'CANCEL', 'NO_ACTION'")
    # 如果是 CANCEL，需要填 ID
    cancel_order_id: str = Field(description="如果要撤单，填入对应的 order_id，否则留空", default="")
    # 如果是 OPEN，填以下参数
    entry_price: float = Field(description="入场价格")
    amount: float = Field(description="开仓数量 (单位: 币的个数，请根据建议下单价值 USDT / Price 计算得出)")
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

# 2. 定义状态 (增加 symbol 字段)
class AgentState(TypedDict):
    symbol: str  # <--- 核心：当前正在分析哪个币
    messages: List[BaseMessage]
    market_context: dict
    account_context: dict
    history_context: List[dict]
    final_output: dict

market_tool = MarketTool()
llm = ChatOpenAI(model="qwen3-max-preview", temperature=0.5).with_structured_output(AgentOutput)

# --- Nodes ---
TRADING_MODE = "REAL"  # "REAL" or "MOCK"
LEVERAGE = 10           # 3倍杠杆
RISK_PER_TRADE_PCT = 0.1 # 每次交易使用 5% 的本金



def start_node(state: AgentState):
    symbol = state['symbol']
    print(f"\n--- [Node] Start: Analyzing {symbol} ({TRADING_MODE} Mode) ---")
    
    # 1. 获取数据 (带 symbol)
    market_full = market_tool.get_market_analysis(symbol)
    account_data = market_tool.get_account_status(symbol)
    recent_summaries = database.get_recent_summaries(symbol, limit=3)
    
    balance = account_data.get('balance', 0) # 例如 1000 USDT
    trade_size_usdt = balance * RISK_PER_TRADE_PCT * LEVERAGE 
    # 如果是 Mock 模式，给一个假定值
    if TRADING_MODE == 'MOCK':
        balance = 10000
        trade_size_usdt = 1000 # 假定每次开 1000 U


    # 2. 数据清洗 (同之前逻辑)
    market_context_llm = {
        "symbol": symbol,
        "sentiment": market_full.get("sentiment"),
        "analysis": {}
    }
    if "analysis" in market_full:
        for tf, data in market_full["analysis"].items():
            if data:
                clean = data.copy()
                if "df_raw" in clean: del clean["df_raw"]
                market_context_llm["analysis"][tf] = clean

    history_text = "\n".join([f"[{s['timestamp']}] {s['content']}" for s in recent_summaries])

    return {
        "market_context": market_full,
        "account_context": account_data,
        "history_context": recent_summaries,
        "messages": [SystemMessage(content=f"""
        你是专业的加密货币量化交易 Agent。你正在分析 **{symbol}**。
    做单尽量做短中线的，只做信心分数高的，超过一定时长没有实际挂单会有惩罚，你只能15m盯一次盘。
    【交易模式】: **{TRADING_MODE}** (请严格遵守风控)
    【资金管理】: 
    - 总权益: {balance:.2f} USDT
    - 杠杆: {LEVERAGE}x
    - 建议单笔下单价值: {trade_size_usdt:.2f} USDT
    - 注意：在输出 amount 时，请计算 {symbol} 的数量 (例如: {trade_size_usdt} / EntryPrice)。
        【当前持仓 (Real Positions)】:
        {json.dumps(account_data['real_positions'], ensure_ascii=False)}

        【当前挂单 (Mock Orders)】:
        {json.dumps(account_data['mock_open_orders'], ensure_ascii=False)}

        【全量市场数据】:
        {json.dumps(market_context_llm, ensure_ascii=False)}
        
        【指标说明】:
        【Volume Profile 核心指标定义】
1. 基准线 (Key Levels)

POC (控制点):

定义: 当前周期成交量最大的价格（市场公允价）。

逻辑:

价格 > POC: 多头优势区，POC 为下方强支撑。

价格 < POC: 空头优势区，POC 为上方强阻力。

回归: 价格远离后常有回归 POC 的磁吸效应。

VAH (价值区上沿):

定义: 70% 成交量区域的顶部边界。

逻辑: 强阻力位。若放量突破并站稳，视为趋势由震荡转为单边上涨的信号。

VAL (价值区下沿):

定义: 70% 成交量区域的底部边界。

逻辑: 强支撑位。震荡行情中的买入点；若放量跌破，视为趋势转为单边下跌。

2. 结构特征 (Structure Nodes)

HVN (高量节点/波峰):

特征: 筹码密集区，也是共识区。

行为: 价格进入此区域会减速、震荡或反转。

策略: 视为强支撑/阻力，适合作为进场点或止盈点。

LVN (低量节点/波谷):

特征: 筹码真空区，流动性稀薄。

行为: 价格进入此区域会加速通过（滑点大，停留短）。

策略: 不可作为支撑阻力。适合作为止损位（因为一旦进入容易直接穿过）或突破后的目标位。

        【历史回顾】:
        {history_text}

        任务：
        1. 总结 {symbol} 的行情。
        2. 管理挂单：
           - 如果有旧的模拟单不再合理，请执行 'CANCEL'。
           - 如果有新的交易机会，请执行 'BUY_LIMIT' 或 'SELL_LIMIT'。
           - 必须设置止盈止损。
        """)]
    }

def agent_node(state: AgentState):
    print(f"--- [Node] Agent: Thinking {state['symbol']} ---")
    response = llm.invoke(state['messages'])
    return {"final_output": response.dict()}


def execution_node(state: AgentState):
    print(f"--- [Node] Execution: Mock Trading {state['symbol']} ---")
    output = state['final_output']
    summary = output['summary']
    orders = output['orders']
    symbol = state['symbol']
    
    # 1. 保存总结
    content = f"Trend: {summary['current_trend']}\nLevels: {summary['key_levels']}"
    database.save_summary(symbol, content, summary['strategy_thought'])
    REAL_TRADE_WHITELIST = ["ETH/USDT"] 

    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': continue
        # --- 撤单逻辑 ---
        elif action == 'CANCEL':
            cancel_id = order.get('cancel_order_id')
            if cancel_id:
                database.cancel_mock_order(cancel_id)
                print(f"🚫 [Mock] Cancelled Order: {cancel_id}")
                # 撤单通常不需要写进 Log 表，除非你想在 Dashboard 看到“撤单记录”
                # 如果想看，可以写进一个专门的 order_history 表，这里暂且跳过，避免混淆 active orders
                
        # --- 开单逻辑 ---
        elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
            side = 'buy' if 'BUY' in action else 'sell'
            
            # A. 写入 Mock 系统 (活跃单池)
            new_id = database.create_mock_order(
                symbol=symbol,
                side=side,
                price=order['entry_price'],
                amount=order['amount'],
                sl=order['stop_loss'],
                tp=order['take_profit']
            )
            
            # B. 写入 Log 系统 (Dashboard 展示用)
            database.save_order_log(
                symbol=symbol,
                side=side,
                entry=order['entry_price'],
                tp=order['take_profit'],
                sl=order['stop_loss'],
                reason=order['reason']
            )
            
            print(f"✅ [Mock & Log] Created Order {new_id}: {side} {symbol} @ {order['entry_price']}")

        if TRADING_MODE == 'REAL' and symbol in REAL_TRADE_WHITELIST:
            print(f"🚀 [REAL TRADE] Executing {action} for {symbol}")
            try:
                # 执行实盘下单
                market_tool.place_real_order(
                    symbol=symbol,
                    action=action, # 'BUY_LIMIT' 或 'SELL_LIMIT' 或 'CANCEL'
                    order_params=order
                )
            except Exception as e:
                print(f"❌ Real Trade Execution Error: {e}")

    return state


# --- Graph ---
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
    """为特定币种运行一次 Agent"""
    initial_state = {
        "symbol": symbol,  # <--- 注入币种
        "messages": []
    }
    app.invoke(initial_state)