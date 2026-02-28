import uuid
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.agent_models import OpenOrderReal, OpenOrderStrategy, CloseOrder
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
# 工具参数 Schema 定义 (用于精简 LLM 看到的参数)
# ==========================================

class OpenRealSchema(BaseModel):
    orders: List[OpenOrderReal] = Field(description="开多或开空指令列表")

class CloseRealSchema(BaseModel):
    orders: List[CloseOrder] = Field(description="平仓指令列表")

class CancelRealSchema(BaseModel):
    order_ids: List[str] = Field(description="要撤销的订单 ID 列表")

class OpenStrategySchema(BaseModel):
    orders: List[OpenOrderStrategy] = Field(description="模拟开仓指令列表")

class CancelStrategySchema(BaseModel):
    order_ids: List[str] = Field(description="要撤销的模拟订单 ID 列表")

# ==========================================
# 1. 实盘模式工具 (REAL Mode Tools)
# ==========================================

@tool(args_schema=OpenRealSchema)
def open_position_real(orders: List[OpenOrderReal], config_id: str, symbol: str):
    """
    【开仓：限价做多或做空】
    仅在执行 BUY_LIMIT (做多) 或 SELL_LIMIT (做空) 时调用。
    暂不支持自动止盈止损，需要使用平仓工具配合实现。
    """
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            # 兼容 dict 格式
            if isinstance(op, dict):
                op = OpenOrderReal(**op)
            
            action = op.action
            price = op.entry_price
            
            latest = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在，跳过。")
                continue
            
            res = market_tool.place_real_order(symbol, action, op.model_dump(), agent_name=config_id)
            if res and 'id' in res:
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', price, 0, 0, op.reason, trade_mode="REAL", config_id=config_id)
                execution_results.append(f"✅ [下单成功] {action} {symbol} @ {price} (Qty: {op.amount})")
            else:
                execution_results.append(f"❌ [Error] {action} 下单失败")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
            
    return "\n".join(execution_results)

@tool(args_schema=CloseRealSchema)
def close_position_real(orders: List[CloseOrder], config_id: str, symbol: str):
    """
    【平仓：挂单平掉现有持仓】
    只有持仓存在时才可以调用。
    持仓时希望止盈、止损时，调用此工具。
    """
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            # 兼容 dict 格式
            if isinstance(op, dict):
                op = CloseOrder(**op)
            
            market_tool.place_real_order(symbol, 'CLOSE', op.model_dump(), agent_name=config_id)
            database.save_order_log("CLOSE_CMD", symbol, agent_name, f"CLOSE_{op.pos_side}", op.entry_price, 0, 0, op.reason, trade_mode="REAL", config_id=config_id)
            execution_results.append(f"✅ 下单成功 ({op.pos_side}) @ {op.entry_price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 下单失败: {str(e)}")
            
    return "\n".join(execution_results)

@tool(args_schema=CancelRealSchema)
def cancel_orders_real(order_ids: List[str], config_id: str, symbol: str):
    """
    【撤单：撤销现有挂单】
    当现有挂单已不符合逻辑时调用。
    """
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


# ==========================================
# 2. 策略/模拟模式工具 (STRATEGY Mode Tools)
# ==========================================

@tool(args_schema=OpenStrategySchema)
def open_position_strategy(orders: List[OpenOrderStrategy], config_id: str, symbol: str):
    """
    【策略开仓：记录模拟交易】
    在模拟模式下记录开多或开空指令，支持止盈止损。
    """
    from config import config as global_config
    agent_config = global_config.get_config_by_id(config_id)
    # 统一使用 config_id 作为数据库中的唯一标识
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            # 兼容 dict 格式
            if isinstance(op, dict):
                op = OpenOrderStrategy(**op)
            
            action = op.action
            price = op.entry_price
            
            latest = market_tool.get_account_status(symbol, is_real=False, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('mock_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate Strategy] {action} @ {price} 已存在。")
                continue
            
            expire_at = (datetime.now() + timedelta(hours=op.valid_duration_hours)).timestamp()
            mock_id = f"ST-{uuid.uuid4().hex[:6]}"
            database.create_mock_order(symbol, 'BUY' if 'BUY' in action else 'SELL', price, op.amount, op.stop_loss or 0, op.take_profit or 0, agent_name=agent_name, config_id=config_id, order_id=mock_id, expire_at=expire_at)
            database.save_order_log(mock_id, symbol, agent_name, 'BUY' if 'BUY' in action else 'SELL', price, op.take_profit or 0, op.stop_loss or 0, f"[Strategy] {op.reason}", trade_mode="STRATEGY", config_id=config_id)
            
            execution_results.append(f"✅ [Executed Strategy] {action} {symbol} @ {price} (Qty: {op.amount})")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
            
    return "\n".join(execution_results)

@tool(args_schema=CancelStrategySchema)
def cancel_orders_strategy(order_ids: List[str], config_id: str, symbol: str):
    """
    【策略撤单：撤销模拟挂单】
    """
    # 统一使用 config_id 作为数据库中的唯一标识
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
