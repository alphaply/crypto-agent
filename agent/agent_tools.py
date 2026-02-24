import uuid
from datetime import datetime, timedelta
from langchain_core.tools import tool
from agent.agent_models import OrderParams
import database
from utils.market_data import MarketTool
from utils.logger import setup_logger

logger = setup_logger("AgentTools")

def _is_duplicate_real_order(new_action, new_price, current_open_orders):
    """检查是否存在重复的实盘挂单。"""
    if new_action not in ['BUY_LIMIT', 'SELL_LIMIT']: return False
    new_side = 'buy' if 'BUY' in new_action else 'sell'
    for existing in current_open_orders:
        if existing.get('side', '').lower() != new_side: continue
        exist_price = float(existing.get('price', 0))
        if exist_price > 0 and abs(exist_price - new_price) / exist_price < 0.001:
            return True
    return False

@tool
def open_position(orders: list, config_id: str, symbol: str, trade_mode: str):
    """
    开仓工具。用于限价做多 (BUY_LIMIT) 或限价做空 (SELL_LIMIT)。
    每个订单应包含: action (BUY_LIMIT/SELL_LIMIT), reason, entry_price, amount, take_profit, stop_loss, valid_duration_hours。
    """
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for o_raw in orders:
        try:
            op = OrderParams(**o_raw)
            if op.action not in ['BUY_LIMIT', 'SELL_LIMIT']:
                execution_results.append(f"Skipped {op.action}: Use close_position for closing.")
                continue
                
            if trade_mode == 'REAL':
                latest = market_tool.get_account_status(symbol, is_real=True, agent_name=agent_name)
                if _is_duplicate_real_order(op.action, op.entry_price, latest.get('real_open_orders', [])):
                    execution_results.append(f"Ignored duplicate real order: {op.action} @ {op.entry_price}")
                    continue
                res = market_tool.place_real_order(symbol, op.action, op.model_dump(), agent_name=agent_name)
                if res and 'id' in res:
                    database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in op.action else 'sell', op.entry_price, op.take_profit, op.stop_loss, op.reason, trade_mode="REAL")
            else:
                latest = market_tool.get_account_status(symbol, is_real=False, agent_name=agent_name)
                if _is_duplicate_real_order(op.action, op.entry_price, latest.get('mock_open_orders', [])):
                    execution_results.append(f"Ignored duplicate mock order: {op.action} @ {op.entry_price}")
                    continue
                expire_at = (datetime.now() + timedelta(hours=op.valid_duration_hours or 24)).timestamp()
                mock_id = f"ST-{uuid.uuid4().hex[:6]}"
                database.create_mock_order(symbol, 'BUY' if 'BUY' in op.action else 'SELL', op.entry_price, op.amount, op.stop_loss, op.take_profit, agent_name=agent_name, order_id=mock_id, expire_at=expire_at)
                database.save_order_log(mock_id, symbol, agent_name, 'BUY' if 'BUY' in op.action else 'SELL', op.entry_price, op.take_profit, op.stop_loss, f"[Strategy] {op.reason}", trade_mode="STRATEGY")
            execution_results.append(f"Successfully opened {op.action} @ {op.entry_price}")
        except Exception as e:
            execution_results.append(f"Failed to open position: {str(e)}")
    return "".join(execution_results)

@tool
def close_position(orders: list, config_id: str, symbol: str, trade_mode: str):
    """
    平仓工具。用于平多或平空 (CLOSE)。
    每个订单应包含: action (CLOSE), reason, entry_price, amount, pos_side (LONG/SHORT)。
    """
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    for o_raw in orders:
        try:
            op = OrderParams(**o_raw)
            if op.action != 'CLOSE':
                execution_results.append(f"Skipped {op.action}: Use open_position for opening.")
                continue
                
            if trade_mode == 'REAL':
                market_tool.place_real_order(symbol, 'CLOSE', op.model_dump(), agent_name=agent_name)
                database.save_order_log("CLOSE_CMD", symbol, agent_name, "CLOSE", op.entry_price, 0, 0, op.reason, trade_mode="REAL")
            else:
                database.save_order_log("CLOSE_MOCK", symbol, agent_name, "CLOSE", op.entry_price, 0, 0, f"[Strategy] {op.reason}", trade_mode="STRATEGY")
            execution_results.append(f"Successfully closed {op.pos_side} @ {op.entry_price}")
        except Exception as e:
            execution_results.append(f"Failed to close position: {str(e)}")
    return "".join(execution_results)

@tool
def cancel_orders(order_ids: list, config_id: str, symbol: str, trade_mode: str):
    """
    撤单工具。用于取消指定的挂单。
    参数 order_ids 为要取消的订单 ID 列表。
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
            execution_results.append(f"Successfully cancelled order: {oid}")
        except Exception as e:
            execution_results.append(f"Failed to cancel order {oid}: {str(e)}")
    return "".join(execution_results)
