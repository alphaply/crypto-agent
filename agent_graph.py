import json
import os
import time
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
# 1. 定义 Pydantic 输出结构 (Schema)
# ==========================================

class OrderParams(BaseModel):
    """交易指令结构"""
    action: str = Field(
        description="动作: 'BUY_LIMIT' (做多), 'SELL_LIMIT' (做空), 'CANCEL' (撤单), 'CLOSE' (平仓), 'NO_ACTION' (观望)",
        pattern="^(BUY_LIMIT|SELL_LIMIT|CANCEL|CLOSE|NO_ACTION)$"
    )
    cancel_order_id: str = Field(description="撤单时填入 ID，否则留空", default="")
    entry_price: float = Field(description="挂单价格")
    amount: float = Field(description="下单数量 (币的个数，非 USDT 金额)")
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
# 3. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    
    # 1. 获取模式
    is_real_trade = config.get('real_trade', False)
    mode_str = "REAL" if is_real_trade else "MOCK"
    
    print(f"\n--- [Node] Start: Analyzing {symbol} using {config.get('model')} ({mode_str} Mode) ---")

    try:
        # 2. 获取数据 (增加异常捕获)
        market_full = market_tool.get_market_analysis(symbol)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_trade)
        recent_summaries = database.get_recent_summaries(symbol, limit=10)
    except Exception as e:
        print(f"❌ [Data Fetch Error]: {e}")
        # 返回空数据防止崩溃，实际生产中可能需要在这里终止或重试
        market_full = {}
        account_data = {'balance': 0, 'real_open_orders': [], 'mock_open_orders': [], 'real_positions': []}
        recent_summaries = []
    
    # 3. 计算资金
    leverage = int(os.getenv('LEVERAGE', 10))
    risk_pct = float(os.getenv('RISK_PER_TRADE_PCT', 0.05))
    balance = account_data.get('balance', 0)
    
    # 模拟盘兜底资金逻辑
    if not is_real_trade and balance < 10: 
        balance = 10000 
    
    trade_size_usdt = balance * risk_pct * leverage

    # 4. 构建订单上下文
    if is_real_trade:
        raw_orders = account_data.get('real_open_orders', [])
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

    # 5. 提取指标数据
    analysis_data = market_full.get("analysis", {}).get("15m", {})
    current_price = analysis_data.get("price", 0)
    # ATR 容错: 如果获取失败，使用价格的 1% 作为兜底
    atr_15m = analysis_data.get("atr", current_price * 0.01) if current_price > 0 else 0
    
    indicators_summary = {}
    for tf in ['5m', '15m', '1h', '4h', '1d']:
        tf_data = market_full.get("analysis", {}).get(tf)
        if tf_data:
            vp_data = tf_data.get("vp", {})
            indicators_summary[tf] = {
                "price": tf_data.get("price"),
                "recent_closes": tf_data.get("recent_closes", [])[-5:], # 只取最后5根
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
    
    history_text = "\n".join([
        f"[{s.get('timestamp')}] Agent: {s.get('agent_name', 'Unknown')}\nLogic: {s.get('strategy_logic', '')[:100]}..." 
        for s in recent_summaries
    ])
    
    now = datetime.now().astimezone() 
    full_time_str = now.strftime('%Y-%m-%d %H:%M:%S %A (%Z UTC%z)')
    
    system_prompt = f"""
你是由 {config.get('model')} 驱动的 **高胜率稳健日内波段交易员**。
当前监控: **{symbol}** | 时间: {full_time_str} | 模式: {mode_str} | 杠杆: {leverage}x
当前市场 15m ATR (波动率): {atr_15m:.2f}

【核心任务】
捕捉日内 结构清晰 的波段机会。你的目标是稳定盈利，而非频繁刷单。
如果市场出现符合策略的高盈亏比机会，你却因为过度犹豫而选择观望，将被视为严重失职。

【技术分析逻辑】
指标 | 形象理解 | 核心逻辑
--- | --- | ---
POC | 成交磁铁 | 价格最喜欢待的地方。价格在它下方是阻力；在它上方是支撑。
VAH/VAL | 价值边界 | 跌破 VAL 叫超跌/看空，涨破 VAH 叫超涨/看多。
HVN | 防御工事 | 历史成交密集峰。价格很难一次性穿过去，是止损/止盈的最佳参考。
ATR | 呼吸频率 | 止损必须大于 1 倍 ATR，否则会被市场噪音误伤。
EMA | 天气预报 | 均线向下是雨天(空)，向上是晴天(多)。逆势做单需极强理由。

【风控与执行规则 (Strict Rules)】
1. **防扫损 (Anti-Sweep)**: 
   - 禁止将止损正好设在支撑/阻力线上。
   - 结构化止损：止损必须放在当前 VAL 或 最近 HVN 之下至少 0.5 * ATR 的位置。
2. **盈亏比 (R/R)**: 
   - 预期 R/R < 2.0 禁止入场。
   - 必须有足够的空间让利润奔跑。
3. **胜率信心**: 仅在信心 > 70% 时出手。
4. **持仓定力**: 一旦根据 VA/HVN 结构入场，禁止仅因为微小波动离场，除非实体跌破结构位。

【资金状态】
- 可用余额: {balance:.2f} USDT
- 建议单笔名义价值: {trade_size_usdt:.2f} USDT
$$ amount = \\frac{{Target Value}}{{Entry Price}} $$
*例如：如果建议价值是 100 USDT，入场价是 880，则 amount = 100 / 880 ≈ 0.1136*
**注意：orders 中的 amount 字段必须填写 币的个数，严禁填写 USDT 金额！**

【现有持仓】
{json.dumps(account_data.get('real_positions', []), ensure_ascii=False)}

{orders_context_str}

【全量市场数据】
{json.dumps(market_context_llm, ensure_ascii=False)}

【历史思路回溯】
{history_text}

【输出指令】
1. **决策**: BUY_LIMIT / SELL_LIMIT / CANCEL / CLOSE / NO_ACTION
2. **Reason**: 必须明确指出参考了哪个 **HVN/POC/VAL/VAH** 点位，并说明 **ATR缓冲** 和 **R/R计算** 过程。
3. **盘面总结**: 包含短中线趋势判断、关键阻力支撑位、以及对后续行情的预测路径。

请严格按 JSON 格式输出，不要包含额外的 Markdown 代码块标记。
"""

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
        
        # 调用 LLM
        response = current_llm.invoke(state['messages'])
        
        # Pydantic v2 使用 model_dump()
        return {**state, "final_output": response.model_dump()}
        
    except Exception as e:
        print(f"❌ [LLM Error] ({symbol}): {e}")
        # 发生错误时返回一个空的安全对象
        error_output = {
            "summary": {
                "current_trend": "Error", 
                "key_levels": "N/A", 
                "strategy_thought": f"LLM Generation Failed: {str(e)}", 
                "predict": "Wait"
            }, 
            "orders": []
        }
        return {**state, "final_output": error_output}

def execution_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    is_real_trade = config.get('real_trade', False)
    agent_name = config.get('model', 'Unknown')
    
    print(f"--- [Node] Execution: Processing {symbol} ---")
    
    output = state['final_output']
    # 再次做安全检查，防止 output 为 None
    if not output:
        print("⚠️ [Execution] No output from Agent, skipping.")
        return state

    summary = output.get('summary', {})
    orders = output.get('orders', [])
    
    # 1. 保存分析日志
    content = f"Trend: {summary.get('current_trend')}\nLevels: {summary.get('key_levels')}\nPredict: {summary.get('predict')}"
    try:
        # 尝试调用旧接口，如果参数不匹配则捕获 (假设 database 接口不确定)
        try:
            database.save_summary(symbol, agent_name, content, summary.get('strategy_thought'))
        except TypeError:
            database.save_summary(symbol, content, summary.get('strategy_thought'))
    except Exception as db_err:
        print(f"⚠️ [DB Error] Save summary failed: {db_err}")

    # 2. 执行订单
    for order in orders:
        action = order['action'].upper()
        if action == 'NO_ACTION': 
            continue
            
        # --- A. 撤单 (CANCEL) ---
        if action == 'CANCEL':
            cancel_id = order.get('cancel_order_id')
            if cancel_id:
                if is_real_trade:
                    print(f"🔄 [REAL] Requesting Cancel: {cancel_id}")
                    real_res = market_tool.place_real_order(symbol, 'CANCEL', order)
                    if not real_res:
                        print(f"❌ [REAL] Cancel failed")
                        continue 
                
                # 模拟盘撤单 + 日志记录
                try:
                    database.cancel_mock_order(cancel_id)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}")
                except Exception as e:
                    print(f"⚠️ [DB Error] Cancel log: {e}")

        # --- B. 平仓 (CLOSE) ---
        elif action == 'CLOSE':
            print(f"🎯 [Action] Close Position: {symbol}")
            reason_log = order.get('reason', 'Auto Close')
            if is_real_trade:
                market_tool.place_real_order(symbol, 'CLOSE', order)
            
            database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", 
                                    order.get('entry_price', 0), 0, 0, reason_log)

        # --- C. 开仓 (LIMIT) ---
        elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
            side = 'buy' if 'BUY' in action else 'sell'
            price = order.get('entry_price')
            
            # 1. 实盘 API 级查重 (防重复下单)
            if is_real_trade:
                existing = state['account_context'].get('real_open_orders', [])
                is_duplicate = False
                for o in existing:
                    # 简单判断：同方向且价格差距小于 0.5%
                    if o['side'].lower() == side and str(o.get('type')).upper() == 'LIMIT':
                        existing_price = float(o.get('price', 0))
                        if price and abs(existing_price - price) / price < 0.005: 
                            is_duplicate = True
                            print(f"⚠️ [Risk Control] 拦截实盘重复单: {side} @ {existing_price}")
                            break
                if is_duplicate: 
                    continue 

            # 2. 执行下单
            final_order_id = None
            
            if is_real_trade:
                # === 实盘分支 ===
                print(f"🚀 [REAL TRADE] Submitting: {symbol} {action} @ {price}")
                real_result = market_tool.place_real_order(symbol, action, order)
                
                if real_result and 'id' in real_result:
                    final_order_id = str(real_result['id'])
                    print(f"✅ [REAL SUCCESS] ID: {final_order_id}")
                else:
                    print(f"❌ [REAL FAIL] Submission failed.")
                    continue 
            else:
                # === 模拟分支 ===
                try:
                    final_order_id = database.create_mock_order(
                        symbol, side, 
                        order['entry_price'], 
                        order['amount'], 
                        order['stop_loss'], 
                        order['take_profit']
                    )
                except Exception as e:
                    print(f"❌ [Mock DB Error]: {e}")

            # 3. 记录历史日志
            if final_order_id:
                log_note = order.get('reason', '')
                if is_real_trade:
                    log_note = f"[RealTrade] {log_note}"
                
                try:
                    database.save_order_log(
                        final_order_id, symbol, agent_name, side, 
                        order['entry_price'], 
                        order['take_profit'], 
                        order['stop_loss'], 
                        log_note
                    )
                except Exception as e:
                    print(f"⚠️ [DB Error] Order Log: {e}")

    return state

# ==========================================
# 4. Graph 编译与运行
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
    is_real_trade = config.get('real_trade', False)
    
    # 修复了之前硬编码 "REAL" 的 bug
    mode_str = "REAL" if is_real_trade else "MOCK"
    
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