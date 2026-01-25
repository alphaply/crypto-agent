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

import database
from market_data import MarketTool

load_dotenv()
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
    reason: str = Field(description="简短的决策理由，必须包含 R/R 计算")

class MarketSummaryParams(BaseModel):
    """行情分析总结"""
    current_trend: str = Field(description="趋势判断 (Bullish/Bearish/Range/Volatile)")
    key_levels: str = Field(description="关键支撑与阻力位")
    strategy_thought: str = Field(description="详细的思维链分析")
    predict: str = Field(description="对未来行情的预测与建议")

class AgentOutput(BaseModel):
    summary: MarketSummaryParams
    orders: List[OrderParams]

class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    agent_config: dict
    market_context: dict
    account_context: dict
    history_context: List[dict]
    final_output: dict

# ==========================================
# 4. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState):
    symbol = state['symbol']
    config = state['agent_config']
    
    # 1. 获取模式
    is_real_trade = config.get('real_trade', False)
    mode_str = "REAL" # 强制告诉llm实盘
    
    print(f"\n--- [Node] Start: Analyzing {symbol} using {config.get('model')} ({mode_str} Mode) ---")

    # 2. 获取数据 (传入 is_real 参数)
    market_full = market_tool.get_market_analysis(symbol)
    account_data = market_tool.get_account_status(symbol, is_real=is_real_trade)
    recent_summaries = database.get_recent_summaries(symbol, limit=10)
    
    # 3. 计算资金
    leverage = int(os.getenv('LEVERAGE', 10))
    risk_pct = float(os.getenv('RISK_PER_TRADE_PCT', 0.05))
    balance = account_data.get('balance', 0)
    # 模拟盘如果余额为0，给个默认值防止报错
    if not is_real_trade and balance < 10: balance = 10000 
    trade_size_usdt = balance * risk_pct * leverage

    # 4. 构建订单上下文
    if is_real_trade:
        raw_orders = account_data.get('real_open_orders', [])
        # 精简字段给 LLM
        display_orders = [{
            "id": o.get('order_id'),
            "side": o.get('side'),
            "type": o.get('type'),
            "price": o.get('price'),
            "amount": o.get('amount')
        } for o in raw_orders]
        orders_context_str = f"【挂单 (Real Orders)】:\n{json.dumps(display_orders, ensure_ascii=False)}"
    else:
        display_orders = account_data.get('mock_open_orders', [])
        orders_context_str = f"【挂单】:\n{json.dumps(display_orders, ensure_ascii=False)}"

    # 5. 提取 ATR (用于防扫损)
    # 尝试从 15m 或 1h 数据中获取 ATR，如果没有则估算
    current_price = market_full.get("analysis", {}).get("15m", {}).get("price", 0)
    atr_15m = market_full.get("analysis", {}).get("15m", {}).get("atr", current_price * 0.01) # 默认1%
    
    market_context_llm = {
        "price": current_price,
        "atr_15m": atr_15m, # ✅ 注入 ATR 数据
        "sentiment": market_full.get("sentiment"),
        "analysis_summary": {tf: data.get("vp", {}) for tf, data in market_full.get("analysis", {}).items() if data}
    }
    
    history_text = "\n".join([
        f"[{s['timestamp']}] Agent: {s.get('agent_name', 'Unknown')}\nLogic: {s['strategy_logic'][:512]}..." 
        for s in recent_summaries
    ])
    

    now = datetime.now().astimezone() 
    full_time_str = now.strftime('%Y-%m-%d %H:%M:%S %A (%Z UTC%z)')
    system_prompt = f"""
你是由 {config.get('model')} 驱动的 **高胜率稳健日内波段策略交易员 (Conservative Strategic Trader)**。
当前监控: **{symbol}** | 时间: {full_time_str} | 模式: {mode_str} | 杠杆: {leverage}x
当前市场 15m ATR (波动率参考): {atr_15m:.2f}

周末每1h进行一次行情分析，工作日每15m进行一次分析。

【核心目标】
寻找**高盈亏比 (High R/R)** 且 **结构清晰** 的交易机会。
不要做位于"中间地带"的低质量交易。
美盘时间段行情发展迅速，美盘的时候需要更加谨慎稳健

【防扫损策略 (Anti-Sweep Strategy)】
Crypto 市场充斥着流动性掠夺(Liquidity Sweep)和假突破。你的风控必须包含 ATR 缓冲：
1. **止损设置**: 禁止将止损紧贴支撑/阻力位。
   - 止损价格 = 技术位 +/- (0.5 ~ 1.0 * ATR)。
   - 给市场留出呼吸空间，防止被插针打掉后反向波动。
2. **入场确认**: 
   - 对指标进行盘面解读，寻找支撑/阻力位。
   - 结合多周期 (15m/1h/4h) 趋势共振。

【严格执行规则】
1. **盈亏比 (R/R)**: (Take Profit - Entry) / (Entry - Stop Loss) 尽量 **>= 2.0**。
   - 如果加上 ATR 缓冲后 R/R < 2.0，则**放弃交易 (NO_ACTION)**，不要强行下单。
2. 胜率
    胜率信心必须大于 70%。

【资金状态】
- 可用余额: {balance:.2f} USDT
- 建议单笔名义价值: {trade_size_usdt:.2f} USDT

【现有持仓】
{json.dumps(account_data['real_positions'], ensure_ascii=False)}

{orders_context_str}

【市场数据】
{json.dumps(market_context_llm, ensure_ascii=False)}

【历史总结】
{history_text}


【输出要求】
1. 如果没有极佳机会，请输出 **NO_ACTION**。
2. **Reason** 必须包含：参考的技术位、ATR缓冲是如何考虑的、以及计算出的 R/R 值。
3. 盘面总结需包含：短线/中线趋势判断、关键支撑阻力，对后续行情的预测

请严格按格式输出决策。
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
    
    try:
        current_llm = ChatOpenAI(
            model=config.get('model'),
            api_key=config.get('api_key'),
            base_url=config.get('api_base'),
            temperature=config.get('temperature', 0.5) # 稍微降低温度，增加稳健性
        ).with_structured_output(AgentOutput)
        
        response = current_llm.invoke(state['messages'])
        return {"final_output": response.dict()}
        
    except Exception as e:
        print(f"❌ LLM 调用失败 ({symbol}): {e}")
        return {"final_output": {"summary": {"current_trend": "Error", "key_levels": "", "strategy_thought": str(e)}, "orders": []}}

def execution_node(state: AgentState):
    symbol = state['symbol']
    config = state['agent_config']
    is_real_trade = config.get('real_trade', False)
    
    agent_name = config.get('model', 'Unknown')
    
    print(f"--- [Node] Execution: Processing {symbol} ---")
    
    output = state['final_output']
    summary = output.get('summary', {})
    orders = output.get('orders', [])
    
    content = f"Trend: {summary.get('current_trend')}\nLevels: {summary.get('key_levels')}\nPredict: {summary.get('predict')}"
    try:
        database.save_summary(symbol, agent_name, content, summary.get('strategy_thought'))
    except TypeError:
        database.save_summary(symbol, content, summary.get('strategy_thought'))

    # 2. 执行订单
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': continue
            
        # --- A. 撤单 (CANCEL) ---
        if action == 'CANCEL':
            cancel_id = order.get('cancel_order_id')
            if cancel_id:
                # 1. 如果是实盘，请求交易所撤单
                if is_real_trade:
                    print(f"🔄 [REAL] 正在请求撤单: {cancel_id}")
                    real_res = market_tool.place_real_order(symbol, 'CANCEL', order)
                    if not real_res:
                        print(f"❌ [REAL] 撤单失败，跳过日志记录")
                        continue 
                
                database.cancel_mock_order(cancel_id)
                
                database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}")

        # --- B. 平仓 (CLOSE) ---
        elif action == 'CLOSE':
            print(f"🎯 [Action] 平仓指令: {symbol}")
            if is_real_trade:
                market_tool.place_real_order(symbol, 'CLOSE', order)
            
            database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", order['entry_price'], 0, 0, order['reason'])

        # --- C. 开仓 (LIMIT) ---
        elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
            side = 'buy' if 'BUY' in action else 'sell'
            
            # 1. 实盘 API 级查重 (防手抖)
            if is_real_trade:
                existing = state['account_context'].get('real_open_orders', [])
                is_duplicate = False
                for o in existing:
                    if o['side'].lower() == side and o['raw_type'] == 'LIMIT':
                        price_diff_pct = abs(float(o['price']) - order['entry_price']) / order['entry_price']
                        if price_diff_pct < 0.005: 
                            is_duplicate = True
                            print(f"⚠️ [Risk Control] 拦截实盘重复单: {side} @ {o['price']}")
                            break
                if is_duplicate: continue 

            # 2. 执行下单 (彻底分离实盘和模拟盘逻辑)
            final_order_id = None
            
            if is_real_trade:
                # === 实盘分支 ===
                print(f"🚀 [REAL TRADE] 正在提交: {symbol} {action}")
                real_result = market_tool.place_real_order(symbol, action, order)
                
                if real_result and 'id' in real_result:
                    final_order_id = str(real_result['id'])
                    print(f"✅ [REAL SUCCESS] 实盘下单成功，ID: {final_order_id}")
                else:
                    print(f"❌ [REAL FAIL] 实盘下单失败，不记录日志")
                    continue 
            else:
                # === 模拟分支 ===
                # 仅模拟盘才写入 mock_orders 表
                final_order_id = database.create_mock_order(
                    symbol, side, 
                    order['entry_price'], 
                    order['amount'], 
                    order['stop_loss'], 
                    order['take_profit']
                )

            # 3. 记录历史日志 (Orders Table)
            # 实盘和模拟盘都需要记录这一笔
            if final_order_id:
                log_note = order['reason']
                if is_real_trade:
                    log_note = f"[RealTrade] {log_note}"
                
                # ✅ 修复：传入 agent_name
                database.save_order_log(
                    final_order_id, 
                    symbol, 
                    agent_name, 
                    side, 
                    order['entry_price'], 
                    order['take_profit'], 
                    order['stop_loss'], 
                    log_note
                )

    return state


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
    is_real_trade = config.get('real_trade', False)
    mode_str = "REAL" if is_real_trade else "MOCK"
    
    print(f"\n--- [Node] Start: {symbol} using {config.get('model')} ({mode_str}) ---")

    initial_state = {
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
        print(f"❌ Graph Error for {symbol} ({config.get('model')}): {e}")