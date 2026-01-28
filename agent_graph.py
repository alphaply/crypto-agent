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

TZ_CN = pytz.timezone('Asia/Shanghai')
# 假设 database 和 market_data 模块已存在
import database 
from market_data import MarketTool

# 加载环境变量
load_dotenv()
market_tool = MarketTool()

# ==========================================
# Prompt 模板 (更新：接受 orders_text)
# ==========================================

# A. 实盘执行模式 Prompt
REAL_TRADE_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **高胜率稳健合约交易员**。
当前时间: {current_time}
当前监控: {symbol} | 模式: 实盘交易 | 杠杆: {leverage}x
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
捕捉日内 结构清晰 的波段机会。你的目标是稳定盈利，而非频繁刷单。
如果市场出现符合策略的高盈亏比机会，你却因为过度犹豫而选择观望，将被视为严重失职。
**实盘模式下，你不需要设置止盈止损 (TP/SL)，专注于优异的进场位置与出场位置。**
开单要有明确的信心支撑
做单方式：双向持仓 做多做空均可

【权限与指令】
1. **BUY_LIMIT**: 挂单开多 (价格必须 < 现价)。
2. **SELL_LIMIT**: 挂单开空 (价格必须 > 现价)。
3. **CLOSE**: 挂限价单平多或平空 (Limit Close)。**注意：必须在 `entry_price` 中填入平仓价格**，不要留空。CLOSE只支持限价单。
4. **CANCEL**: 撤销指定的挂单。
5. **NO_ACTION**: 没有极高把握时，保持空仓。

【决策铁律】
1. **点位精准**: 不要在半山腰挂单。
2. **防滑点**: 严禁使用市价开仓/平仓，必须使用 Limit 单。平仓时请计算好想要退出的 Limit 价格。
3. **趋势顺势**: 你尊重中长线指标，但是你是短线稳健性交易员。
4. 仅在信心 > 70% 时出手。
5. 要保持高胜率以及高回报率

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
1. **时效性检查**: 现在的价格 ({current_price}) 是否已经跌破/突破了历史记录中的支撑/阻力位？
2.
   - BUY_LIMIT 入场价格必须 <= {current_price}
   - SELL_LIMIT 入场价格必须 >= {current_price}
   - CLOSE 价格务必合理（多单止盈价 > 现价，空单止盈价 < 现价，或者为了快速跑路选一个接近现价的位置）。
3. 禁止梭哈，单笔下单金额不得超过 可用余额 的 40%。

思路 解读 中文描述
- `action`: BUY_LIMIT / SELL_LIMIT / CLOSE / CANCEL / NO_ACTION
- `pos_side`: 如果是 CLOSE，必须填 'LONG' 或 'SHORT'；其他情况留空
- `entry_price`: 挂单价格 / 平仓价格 (CLOSE 必须填此项)
- `amount`: 下单数量 (注意单位是币的数量而不是USDT的数量)
- `reason`: 简短的执行理由
- `take_profit`: 填 0
- `stop_loss`: 填 0
- `cancel_order_id`: 填要撤销的订单 ID (如8389766084576502933)
"""

STRATEGY_PROMPT_TEMPLATE = """
你是由 {model} 驱动的 **资深加密货币策略分析师 (Crypto Strategist)**。
当前时间: {current_time}
当前监控: {symbol} | 模式: 策略分析 (STRATEGY IDEA)
当前价格: {current_price} | 15m ATR: {atr_15m:.2f}

【角色任务】
你需要分析中长线趋势，生成具有高盈亏比 (R/R Ratio) 的交易计划。(4h级别日线级别)
你要做的是长线趋势单策略，而非频繁短线交易。
长线趋势单精准接针是一个非常重要的技能。
**策略模式下，必须明确给出 止损(SL) 和 止盈(TP) 点位。**

【策略要求】
1. **盈亏比**: 预期 R/R 必须 > 2.0。（越高越好）胜率也是一样的。
2. **逻辑支撑**: 必须基于结构位 (Structure)、供需区 (Supply/Demand) 或流动性 (Liquidity) 制定计划。
3. **完整性**: 必须包含入场价、止损价、止盈价。
4. 你捕捉的是中长线趋势，稳健是你的目标，要稳稳赚钱。
5. **动态调整**: 请检查下方的【活跃策略挂单】，如果之前的挂单逻辑已失效（如价格已远离或趋势改变），**请务必输出 CANCEL 指令**来清理旧单。
6. 仅在信心 > 80% 时出手。
7. 要保持高胜率以及高回报率

【当前状态】
现有持仓: 
{positions_text}

活跃策略挂单 (Strategy Orders): 
{orders_text}

【全量市场数据】
{formatted_market_data}

【历史思路回溯 (Context)】
以下是最近的分析记录，请参考过去的时间线和思路演变：
----------------------------------------
{history_text}
----------------------------------------

【输出要求】
思路 解读 中文描述
- `action`: BUY_LIMIT / SELL_LIMIT / CANCEL / NO_ACTION
- `cancel_order_id`: 如果 action 是 CANCEL，请填写要撤销的单据 ID。
- `entry_price`: 建议入场价
- `take_profit`: 建议止盈价 (必填)
- `stop_loss`: 建议止损价 (必填)
- `reason`: 详细的策略逻辑，包含 R/R 计算。
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
    entry_price: float = Field(description="挂单价格 (CLOSE 时为平仓价格)")
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

class AgentState(TypedDict):
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    final_output: Dict[str, Any]

# 2. 格式化工具函数 (Agent Friendly)
def format_positions_to_agent_friendly(positions: list) -> str:
    """
    将复杂的持仓 JSON 转换为 Agent 易读的精简文本
    """
    if not positions:
        return "无持仓 (No Positions)"
    
    lines = []
    for p in positions:
        side = p.get('side', '').upper()
        # 清理 symbol 名字，比如 BNB/USDT:USDT -> BNB/USDT
        sym = p.get('symbol', '').split(':')[0]
        amt = float(p.get('amount', 0))
        entry = float(p.get('entry_price', 0))
        pnl = float(p.get('unrealized_pnl', 0))
        
        pnl_sign = "+" if pnl >= 0 else ""
        
        line = f"[{side}] {sym} | Amt: {amt} | Entry: {entry} | PnL: {pnl_sign}{pnl:.3f}"
        lines.append(line)
        
    return "\n".join(lines)

def format_orders_to_agent_friendly(orders: list) -> str:
    """
    将活跃挂单转换为 Agent 易读的精简文本
    输入样例: [{"id": "84862268134", "side": "buy", "type": "限价入场", "price": 873.5, "amount": 0.01}]
    输出样例: [BUY] LIMIT | ID: 84862268134 | Price: 873.5 | Amt: 0.01
    """
    if not orders:
        return "无活跃挂单 (No Active Orders)"

    lines = []
    for o in orders:
        # 1. 提取方向
        side = o.get('side', '').upper()
        
        # 2. 标准化类型 (处理中文 "限价入场")
        raw_type = str(o.get('type', 'LIMIT'))
        if '限价' in raw_type or 'limit' in raw_type.lower():
            order_type = 'LIMIT'
        else:
            order_type = raw_type.upper()

        # 3. 提取核心数据
        oid = o.get('id', 'N/A')
        price = float(o.get('price', 0))
        amt = float(o.get('amount', 0))

        # 4. 可选: 止盈止损 (策略单可能会有)
        tp = float(o.get('tp', 0) or o.get('take_profit', 0))
        sl = float(o.get('sl', 0) or o.get('stop_loss', 0))
        
        extras = ""
        if tp > 0 or sl > 0:
            extras = f" | TP: {tp} | SL: {sl}"
        
        line = f"[{side}] {order_type} | ID: '{oid}' | Price: {price} | Amt: {amt}{extras}"
        lines.append(line)

    return "\n".join(lines)

def format_market_data_to_markdown(data: dict) -> str:
    """
    将复杂的市场 JSON 数据转换为 LLM 易读的 Markdown 格式
    (更新：新增 ATR, Vol Status, Recent Closes 列)
    """
    def fmt_price(price):
        if price is None or price == 0: return "0"
        abs_p = abs(price)
        if abs_p >= 1000: return f"{int(price)}"      
        if abs_p >= 1: return f"{price:.2f}"          
        if abs_p >= 0.01: return f"{price:.4f}"       
        return f"{price:.8f}".rstrip('0')              

    def fmt_num(num):
        if num > 1_000_000_000: return f"{num/1_000_000_000:.1f}B"
        if num > 1_000_000: return f"{num/1_000_000:.1f}M"
        if num > 1_000: return f"{num/1_000:.1f}K"
        return f"{num:.0f}"

    current_price = data.get("current_price", 0)
    atr_15m = data.get("atr_15m", 0)
    
    sent = data.get("sentiment", {})
    funding = sent.get("funding_rate", 0) * 100 
    oi = sent.get("open_interest", 0)
    
    vol_24h = fmt_num(sent.get("24h_quote_vol", 0))
    oi_str = fmt_num(oi)
    
    header = (
        f"**Snapshot** | Price: {fmt_price(current_price)} | 15m ATR: {fmt_price(atr_15m)}\n"
        f"Sentiment: Fund: {funding:.4f}% | OI: {oi_str} | Vol24h: {vol_24h}\n"
    )

    table_header = (
        "| TF | Price | ATR | RSI | Vol Status | Recent Closes (Last 5) | EMA (20/50/100/200) | POC | VA Range | HVN |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    
    rows = []
    indicators = data.get("technical_indicators", {})
    all_possible_timeframes = ['5m', '15m', '1h', '4h', '1d', '1w']
    
    for tf in all_possible_timeframes:
        if tf not in indicators: continue
        d = indicators[tf]
        
        # 1. 基础数据
        tf_price = fmt_price(d.get('price', 0))
        atr = fmt_price(d.get('atr', 0))
        rsi = f"{d.get('rsi', 0):.1f}"
        
        # 2. 成交量状态
        vol_stat = d.get('volume_status', 'N/A')
        
        raw_closes = d.get('recent_closes', [])
        # 为了节省 token 和版面，如果价格数字很大，这里只显示最后几位小数可能不够直观，
        # 建议直接用 fmt_price 格式化，逗号分隔
        closes_str = ", ".join([fmt_price(x) for x in raw_closes])
        
        # 4. EMA
        ema = d.get('ema', {})
        e20 = fmt_price(ema.get('ema_20', 0))
        e50 = fmt_price(ema.get('ema_50', 0))
        e100 = fmt_price(ema.get('ema_100', 0))
        e200 = fmt_price(ema.get('ema_200', 0))
        ema_str = f"{e20}/{e50}/{e100}/{e200}"
        
        # 5. VP 数据
        vp = d.get('vp', {})
        poc = fmt_price(vp.get('poc', 0))
        val = fmt_price(vp.get('val', 0))
        vah = fmt_price(vp.get('vah', 0))
        va_range = f"{val}-{vah}"
        
        raw_hvns = vp.get('hvns', [])
        top_hvns = sorted(raw_hvns, reverse=True)[:3]
        hvn_str = ",".join([fmt_price(h) for h in top_hvns])
        
        # 组装行
        row = f"| {tf} | {tf_price} | {atr} | {rsi} | {vol_stat} | {closes_str} | {ema_str} | {poc} | {va_range} | {hvn_str} |"
        rows.append(row)
    
    return header + table_header + "\n".join(rows)
# ==========================================
# 4. Graph 节点逻辑
# ==========================================

def start_node(state: AgentState) -> AgentState:
    symbol = state['symbol']
    config = state['agent_config']
    now = datetime.now(TZ_CN)
    week_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')} ({week_map[now.weekday()]})"
    trade_mode = config.get('mode', 'STRATEGY').upper()
    is_real_exec = (trade_mode == 'REAL')
    
    print(f"\n--- [Node] Start: Analyzing {symbol} | Mode: {trade_mode} ---")

    try:
        # 获取全量数据
        market_full = market_tool.get_market_analysis(symbol, mode=trade_mode)
        # 获取账户数据 (实盘模式读交易所，策略模式读数据库或模拟余额)
        account_data = market_tool.get_account_status(symbol, is_real=is_real_exec)
        # 获取最近历史记录
        recent_summaries = database.get_recent_summaries(symbol, limit=3)
    except Exception as e:
        print(f"❌ [Data Fetch Error]: {e}")
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
    print(f"--- [Node] Agent: {config.get('model')} is thinking for {symbol} ---")
    
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
    
    trade_mode = config.get('mode', 'STRATEGY').upper()
    
    print(f"--- [Node] Execution: {symbol} | Mode: {trade_mode} ---")
    
    output = state['final_output']
    if not output: return state

    summary = output.get('summary', {})
    raw_orders = output.get('orders', [])
    
    # CANCEL(0) > CLOSE(1) > 其他开仓(2) 先平仓 再开仓
    orders = sorted(raw_orders, key=lambda x: 0 if x['action']=='CANCEL' else (1 if x['action']=='CLOSE' else 2))
    
    # 1. 保存分析日志 (通用)
    content = f"[{trade_mode}] Trend: {summary.get('current_trend')}\nPredict: {summary.get('predict')}"
    try:
        database.save_summary(symbol, agent_name, content, summary.get('strategy_thought'))
    except Exception as db_err:
        print(f"⚠️ [DB Error] Save summary failed: {db_err}")

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

        # ==========================================
        # 分支 A: 实盘执行 (REAL)
        # ==========================================
        if trade_mode == 'REAL':
            # 强制清空 TP/SL (实盘暂由人工或另外逻辑控制风控)
            order['take_profit'] = 0
            order['stop_loss'] = 0
            
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    print(f"🔄 [REAL] Cancel: {cancel_id}")
                    market_tool.place_real_order(symbol, 'CANCEL', order)
                    database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {cancel_id}", trade_mode="REAL")

            # 2. 平仓
            elif action == 'CLOSE':
                print(f"🎯 [REAL] Close Position (Limit)")
                close_res = market_tool.place_real_order(symbol, 'CLOSE', order)
                if close_res:
                     database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", order.get('entry_price'), 0, 0, log_reason, trade_mode="REAL")

            # 3. 开仓 (Limit) - ✅ 增加防重检测
            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                # 获取当前实盘挂单
                real_open_orders = state['account_context'].get('real_open_orders', [])
                
                if _is_duplicate_order(action, entry_price, real_open_orders):
                    print(f"🛑 [Filter] 忽略重复实盘挂单: {action} @ {entry_price}")
                    continue # 跳过下单

                print(f"🚀 [REAL] Order: {action} @ {entry_price}")
                res = market_tool.place_real_order(symbol, action, order)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', 
                                            entry_price, 0, 0, log_reason, trade_mode="REAL")

        # ==========================================
        # 分支 B: 策略模式 (STRATEGY)
        # ==========================================
        else:
            # 1. 撤单
            if action == 'CANCEL':
                cancel_id = order.get('cancel_order_id')
                if cancel_id:
                    try:
                        print(f"🔄 [STRATEGY] Cancelling Mock Order: {cancel_id}")
                        database.cancel_mock_order(cancel_id)
                        database.save_order_log(cancel_id, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel: {cancel_id}", trade_mode="STRATEGY")
                    except Exception as e:
                        print(f"⚠️ [Mock Cancel Error]: {e}")

            # 2. 开仓 - ✅ 增加防重检测
            elif action in ['BUY_LIMIT', 'SELL_LIMIT']:
                entry_price = float(order.get('entry_price', 0))
                # 获取当前策略挂单
                mock_open_orders = state['account_context'].get('mock_open_orders', [])

                if _is_duplicate_order(action, entry_price, mock_open_orders):
                    print(f"🛑 [Filter] 忽略重复策略挂单: {action} @ {entry_price}")
                    continue # 跳过入库

                side = 'BUY' if 'BUY' in action else 'SELL'
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                
                print(f"💡 [STRATEGY] Idea: {side} @ {entry_price} | ID: {mock_id}")
                
                # ✅ 2. 存入挂单池 (一定要传 mock_id !!!)
                database.create_mock_order(
                    symbol, side, 
                    entry_price, 
                    order['amount'], 
                    order['stop_loss'], 
                    order['take_profit'],
                    order_id=mock_id  # <--- 关键修改：传入 ID
                )

                # ✅ 3. 存入日志 (使用同一个 mock_id)
                database.save_order_log(
                    mock_id, symbol, agent_name, side, 
                    entry_price, 
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