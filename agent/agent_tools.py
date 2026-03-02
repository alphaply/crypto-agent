import uuid
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.agent_models import OpenOrderReal, OpenOrderSpotDCA, OpenOrderStrategy, CloseOrder
import database
from utils.market_data import MarketTool
from utils.logger import setup_logger

logger = setup_logger("AgentTools")


def _is_duplicate_real_order(new_action, new_price, current_open_orders):
    """防抖逻辑：检查在相同价格区间内是否已存在相同方向的挂单。"""
    if new_action not in ['BUY_LIMIT', 'SELL_LIMIT']: return False
    new_side = 'buy' if 'BUY' in new_action else 'sell'
    for existing in current_open_orders:
        if existing.get('side', '').lower() != new_side: continue
        exist_price = float(existing.get('price', 0))
        # 如果价格差距小于 0.1%，认为是重复挂单
        if exist_price > 0 and abs(exist_price - new_price) / exist_price < 0.001:
            return True
    return False

# ==========================================
# 工具参数 Schema 定义
# ==========================================

class OpenRealSchema(BaseModel):
    orders: List[OpenOrderReal] = Field(description="开多或开空指令列表")

class OpenSpotDCASchema(BaseModel):
    orders: List[OpenOrderSpotDCA] = Field(description="现货定投买入指令列表")

class CloseRealSchema(BaseModel):
    orders: List[CloseOrder] = Field(description="平仓指令列表")

class CancelRealSchema(BaseModel):
    order_ids: List[str] = Field(description="要撤销的订单 ID 列表")

class OpenStrategySchema(BaseModel):
    orders: List[OpenOrderStrategy] = Field(description="模拟开仓指令列表")

class CancelStrategySchema(BaseModel):
    order_ids: List[str] = Field(description="要撤销的模拟订单 ID 列表")

class EventContractOrderSchema(BaseModel):
    direction: Literal["Long", "Short"] = Field(description="开仓方向，多 (Long) 或者 空 (Short)")
    duration: str = Field(description="合约时间期限，例如：30min, 1h, 1d")
    entry_condition: str = Field(description="理想入场位置或者条件，文字描述")

class AnalyzeEventContractSchema(BaseModel):
    pass # 无需参数，系统会自动注入当前 symbol 和 config_id

# ==========================================
# 1. 通用交易工具
# ==========================================

@tool(args_schema=OpenSpotDCASchema)
def open_position_spot_dca(orders: List[OpenOrderSpotDCA], config_id: str, symbol: str):
    """【开仓：现货限价定投买入】仅在执行 BUY_LIMIT (买入) 时调用。"""
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            if isinstance(op, dict): op = OpenOrderSpotDCA(**op)
            action, price = op.action, op.entry_price
            latest = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在。")
                continue
            res = market_tool.place_real_order(symbol, action, op.model_dump(), agent_name=config_id)
            if res and 'id' in res:
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy', price, 0, 0, op.reason, trade_mode="SPOT_DCA", config_id=config_id)
                execution_results.append(f"✅ [下单成功] {action} {symbol} @ {price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 现货开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=OpenRealSchema)
def open_position_real(orders: List[OpenOrderReal], config_id: str, symbol: str):
    """【开仓：限价做多或做空】仅在执行 BUY_LIMIT (做多) 或 SELL_LIMIT (做空) 时调用。"""
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            if isinstance(op, dict): op = OpenOrderReal(**op)
            action, price = op.action, op.entry_price
            latest = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在。")
                continue
            res = market_tool.place_real_order(symbol, action, op.model_dump(), agent_name=config_id)
            if res and 'id' in res:
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', price, 0, 0, op.reason, trade_mode="REAL", config_id=config_id)
                execution_results.append(f"✅ [下单成功] {action} {symbol} @ {price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CloseRealSchema)
def close_position_real(orders: List[CloseOrder], config_id: str, symbol: str):
    """【平仓：挂单平掉现有持仓】。"""
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            if isinstance(op, dict): op = CloseOrder(**op)
            market_tool.place_real_order(symbol, 'CLOSE', op.model_dump(), agent_name=config_id)
            database.save_order_log("CLOSE_CMD", symbol, agent_name, f"CLOSE_{op.pos_side}", op.entry_price, 0, 0, op.reason, trade_mode="REAL", config_id=config_id)
            execution_results.append(f"✅ 下单成功 ({op.pos_side}) @ {op.entry_price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 下单失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CancelRealSchema)
def cancel_orders_real(order_ids: List[str], config_id: str, symbol: str):
    """【撤单：撤销现有挂单】。"""
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for oid in order_ids:
        try:
            market_tool.place_real_order(symbol, 'CANCEL', {"cancel_order_id": oid}, agent_name=config_id)
            database.save_order_log(oid, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {oid}", trade_mode="REAL", config_id=config_id)
            execution_results.append(f"✅ [Cancelled Real] 订单 {oid} 已撤回。")
        except Exception as e:
            execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=OpenStrategySchema)
def open_position_strategy(orders: List[OpenOrderStrategy], config_id: str, symbol: str):
    """【策略开仓：记录模拟交易】。"""
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            if isinstance(op, dict): op = OpenOrderStrategy(**op)
            action, price = op.action, op.entry_price
            latest = market_tool.get_account_status(symbol, is_real=False, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('mock_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate Strategy] {action} @ {price} 已存在。")
                continue
            expire_at = (datetime.now() + timedelta(hours=op.valid_duration_hours)).timestamp()
            mock_id = f"ST-{uuid.uuid4().hex[:6]}"
            database.create_mock_order(symbol, 'BUY' if 'BUY' in action else 'SELL', price, op.amount, op.stop_loss or 0, op.take_profit or 0, agent_name=agent_name, config_id=config_id, order_id=mock_id, expire_at=expire_at)
            database.save_order_log(mock_id, symbol, agent_name, 'BUY' if 'BUY' in action else 'SELL', price, op.take_profit or 0, op.stop_loss or 0, f"[Strategy] {op.reason}", trade_mode="STRATEGY", config_id=config_id)
            execution_results.append(f"✅ [Executed Strategy] {action} {symbol} @ {price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CancelStrategySchema)
def cancel_orders_strategy(order_ids: List[str], config_id: str, symbol: str):
    """【策略撤单：撤销模拟挂单】。"""
    agent_name = config_id
    execution_results = []
    for oid in order_ids:
        try:
            database.cancel_mock_order(oid)
            database.save_order_log(oid, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel", trade_mode="STRATEGY", config_id=config_id)
            execution_results.append(f"✅ [Cancelled Strategy] 订单 {oid} 已撤回。")
        except Exception as e:
            execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
    return "\n".join(execution_results)

# ==========================================
# 2. 专用分析工具
# ==========================================

@tool(args_schema=AnalyzeEventContractSchema)
def analyze_event_contract(*args, **kwargs) -> str:
    """
    [事件合约分析工具] 一次性扫描并返回当前交易对在 30min, 1h, 1d 三个关键时间窗口的客观指标看板。
    
    使用说明：
    1. 此工具【不需要任何参数】。直接调用 `analyze_event_contract()` 即可。
    2. 它会自动处理当前正在讨论的交易对（Symbol）。
    3. 输出包含：趋势状态、VWAP 偏离、RSI、布林带宽度、成交量状态、筹码分布(POC)及支撑压力位。
    4. 适用于：当你需要快速了解多周期市场概况，或用户询问“现在行情如何”、“给我一些指标数据”时。
    """
    from utils.market_data import MarketTool
    # 动态获取当前的注入参数
    symbol = kwargs.get("symbol", "Unknown")
    config_id = kwargs.get("config_id", "Unknown")
    try:
        mt = MarketTool(config_id=config_id)
        # 获取 30m, 1h, 1d 周期数据
        analysis = mt.get_market_analysis(symbol, timeframes=['30m', '1h', '1d'])
        
        results = [f"## {symbol} 事件合约深度扫描报告"]
        for tf in ['30m', '1h', '1d']:
            data = analysis['analysis'].get(tf)
            if not data: continue
            
            price = data.get('price', 0)
            vwap = data.get('vwap', 0)
            bb = data.get('bollinger', {})
            trend = data.get('trend', {}).get('status', 'N/A')
            poc = data.get('vp', {}).get('poc', 0)
            
            # 计算指标详情
            vwap_dist = ((price - vwap) / vwap) * 100 if vwap != 0 else 0
            rsi_data = data.get('rsi_analysis', {})
            rsi = rsi_data.get('rsi', 0)
            vol_status = data.get('volume_analysis', {}).get('status', 'N/A')
            bb_width = bb.get('width', 0) * 100 # 转换为百分比
            
            res = (
                f"### [{tf} 数据看板]\n"
                f"- **当前价**: {price}\n"
                f"- **趋势引擎**: {trend}\n"
                f"- **VWAP 偏离度**: {vwap_dist:.3f}%\n"
                f"- **RSI (14)**: {rsi:.2f}\n"
                f"- **布林带宽度**: {bb_width:.2f}%\n"
                f"- **成交量状态**: {vol_status}\n"
                f"- **筹码分布 (POC)**: {poc}\n"
                f"- **支撑/压力 (BB)**: 上轨 {bb.get('up', 'N/A')} / 下轨 {bb.get('low', 'N/A')}"
            )
            results.append(res)
            
        return "\n\n".join(results)
    except Exception as e:
        return f"Error executing event contract analysis: {str(e)}"

@tool(args_schema=EventContractOrderSchema)
def format_event_contract_order(direction: Literal["Long", "Short"], duration: str, entry_condition: str, symbol: str, config_id: str) -> str:
    """
    [事件合约格式化工具] 专门用于生成事件合约的开单指令格式。事件合约没有止损，到时间自动平仓。
    当用户确认想要进行事件合约交易，或要求输出事件合约开单格式时，使用此工具生成标准化的卡片输出。
    """
    direction_emoji = "🟢 多" if direction == "Long" else "🔴 空"
    
    formatted_msg = (
        f"📋 **事件合约 开单计划**\n"
        f"------------------------\n"
        f"🔹 **交易标的**: {symbol}\n"
        f"🔹 **方向**: {direction_emoji} ({direction})\n"
        f"🔹 **周期**: {duration}\n"
        f"🔹 **入场条件**: {entry_condition}\n"
        f"⚠️ **注意**: 事件合约无止损机制，到期后自动交割平仓。"
    )
    return formatted_msg
