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
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy', price, 0, 0, op.reason, trade_mode="REAL", config_id=config_id)
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

@tool
def analyze_event_contract(symbol: str, config_id: str) -> str:
    """
    [事件合约分析工具] 仅在用户要求对特定时间窗口（30min, 1h, 1d）进行价格方向预测时使用。
    输出：开仓具体点位/价格、预测方向（Long/Short）、预测有效时长。
    """
    from utils.market_data import MarketTool
    try:
        mt = MarketTool(config_id=config_id)
        # 获取 30m, 1h, 1d 周期数据
        analysis = mt.get_market_analysis(symbol, timeframes=['30m', '1h', '1d'])
        
        results = [f"## {symbol} 事件合约深度扫描报告"]
        for tf in ['30m', '1h', '1d']:
            data = analysis['analysis'].get(tf)
            if not data: continue
            
            price = data['price']
            vwap = data['vwap']
            bb = data['bollinger']
            trend = data['trend']['status']
            poc = data['vp']['poc']
            
            # 计算 VWAP 偏离度
            vwap_dist = ((price - vwap) / vwap) * 100
            direction = "BULLISH (看多)" if price > vwap and "Bullish" in trend else "BEARISH (看空)"
            
            res = (
                f"### [{tf} 窗口] 预测分析\n"
                f"- **当前基准价**: {price}\n"
                f"- **建议开仓位**: {poc} (价值中心) 或 {bb['mid']} (布林中轨)\n"
                f"- **核心动能方向**: {direction}\n"
                f"- **指标状态**: Trend={trend}, VWAP偏离={vwap_dist:.3f}%\n"
                f"- **预测有效期**: 未来 {tf}"
            )
            results.append(res)
            
        return "\n\n".join(results)
    except Exception as e:
        return f"Error executing event contract analysis: {str(e)}"
