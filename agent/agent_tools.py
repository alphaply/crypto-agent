import uuid
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.agent_models import OpenOrder, CloseOrder
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

@tool
def open_position(orders: List[OpenOrder], config_id: str, symbol: str, trade_mode: str):
    """
    【交易核心工具：开多或开空】
    仅在执行 BUY_LIMIT (做多) 或 SELL_LIMIT (做空) 指令时调用此工具。
    
    参数规范：
    - action: 必须明确是做多还是做空。
    - entry_price: 建议参考当前 15m/1h 周期的支撑阻力位。
    - amount: 下单数量，请务必参考你的可用余额 (balance) 和杠杆倍数 单位是币而不是USDT。
    - take_profit / stop_loss: 强烈建议设置，用于保护仓位。
    
    系统约束：
    - 此工具严禁用于平仓 (CLOSE) 或撤单 (CANCEL)。
    - 系统会自动处理 symbol 和杠杆，你只需要关注买卖逻辑。
    """
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            action = op.action
            price = op.entry_price
            
            if trade_mode == 'REAL':
                latest = market_tool.get_account_status(symbol, is_real=True, agent_name=agent_name)
                if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                    execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在，跳过。")
                    continue
                
                res = market_tool.place_real_order(symbol, action, op.model_dump(), agent_name=agent_name)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', price, op.take_profit or 0, op.stop_loss or 0, op.reason, trade_mode="REAL")
            else:
                latest = market_tool.get_account_status(symbol, is_real=False, agent_name=agent_name)
                if _is_duplicate_real_order(action, price, latest.get('mock_open_orders', [])):
                    execution_results.append(f"⚠️ [Duplicate Strategy] {action} @ {price} 已存在。")
                    continue
                
                expire_at = (datetime.now() + timedelta(hours=op.valid_duration_hours)).timestamp()
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                database.create_mock_order(symbol, 'BUY' if 'BUY' in action else 'SELL', price, op.amount, op.stop_loss or 0, op.take_profit or 0, agent_name=agent_name, order_id=mock_id, expire_at=expire_at)
                database.save_order_log(mock_id, symbol, agent_name, 'BUY' if 'BUY' in action else 'SELL', price, op.take_profit or 0, op.stop_loss or 0, f"[Strategy] {op.reason}", trade_mode="STRATEGY")
            
            execution_results.append(f"✅ [Executed] {action} {symbol} @ {price} (Qty: {op.amount})")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
            
    return "\n".join(execution_results)

@tool
def close_position(orders: List[CloseOrder], config_id: str, symbol: str, trade_mode: str):
    """
    【交易核心工具：平掉现有仓位】
    仅当你拥有现有仓位 (Positions) 且希望止盈、止损或强制平仓时，调用此工具。
    
    参数规范：
    - pos_side: 必须匹配你要平掉的仓位方向 (LONG 或 SHORT)。
    - entry_price: 平仓的触发价格，通常填入当前市场最新价。
    - amount: 想要平仓的数量（单位是币种而不是USDT)。
    
    系统约束：
    - 严禁在此工具中设置 take_profit 或 stop_loss 参数。
    - 如果没有持仓，调用此工具将不会产生任何效果。
    """
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for op in orders:
        try:
            if trade_mode == 'REAL':
                market_tool.place_real_order(symbol, 'CLOSE', op.model_dump(), agent_name=agent_name)
                database.save_order_log("CLOSE_CMD", symbol, agent_name, f"CLOSE_{op.pos_side}", op.entry_price, 0, 0, op.reason, trade_mode="REAL")
            else:
                database.save_order_log("CLOSE_MOCK", symbol, agent_name, f"CLOSE_{op.pos_side}", op.entry_price, 0, 0, f"[Strategy] {op.reason}", trade_mode="STRATEGY")
            
            execution_results.append(f"✅ [Executed] 平仓成功 ({op.pos_side}) @ {op.entry_price}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 平仓失败: {str(e)}")
            
    return "\n".join(execution_results)

@tool
def cancel_orders(order_ids: List[str], config_id: str, symbol: str, trade_mode: str):
    """
    【交易辅助工具：撤销挂单】
    当现有的挂单 (Orders) 已经不符合当前市场逻辑、或者你想重新调整挂单价格时，先调用此工具撤销旧订单。
    
    参数规范：
    - order_ids: 包含你要撤销的订单 ID 列表。例如: ["8389766110438432057"]
    
    系统约束：
    - 撤销后可以紧接着调用 open_position 设置新的挂单。
    """
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for oid in order_ids:
        try:
            if trade_mode == 'REAL':
                market_tool.place_real_order(symbol, 'CANCEL', {"cancel_order_id": oid}, agent_name=agent_name)
                database.save_order_log(oid, symbol, agent_name, "CANCEL", 0, 0, 0, f"撤单: {oid}", trade_mode="REAL")
            else:
                database.cancel_mock_order(oid)
                database.save_order_log(oid, symbol, agent_name, "CANCEL", 0, 0, 0, f"[Strategy] Cancel", trade_mode="STRATEGY")
            
            execution_results.append(f"✅ [Cancelled] 订单 {oid} 已撤回。")
        except Exception as e:
            execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
            
    return "\n".join(execution_results)
