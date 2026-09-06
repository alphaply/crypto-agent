import json
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from backend.agent.agent_models import OpenOrderReal, OpenOrderSpotDCA, OpenOrderStrategy, CloseOrder
import backend.database as database
from backend.utils.market_data import MarketTool
from backend.utils.logger import setup_logger

logger = setup_logger("AgentTools")
DEFAULT_CANCEL_REASON = "未提供撤单原因"


def _normalize_order_models(orders, model_type):
    """Accept structured lists and JSON-encoded lists from model tool calls."""
    value = orders
    for _ in range(2):
        if not isinstance(value, str):
            break
        text = value.strip()
        if not text:
            raise ValueError("orders 不能为空")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"orders 必须是 JSON 数组: {exc.msg}") from exc

    if isinstance(value, dict) and "orders" in value:
        value = value["orders"]
    elif isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("orders 必须是订单数组")

    normalized = []
    for item in value:
        if isinstance(item, model_type):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(model_type.model_validate(item))
        else:
            raise ValueError(f"订单项必须是对象，实际收到 {type(item).__name__}")
    return normalized


def _cancel_side_from_value(side):
    side_text = str(side or "").upper()
    if "BUY" in side_text or "LONG" in side_text:
        return "CANCEL_BUY"
    if "SELL" in side_text or "SHORT" in side_text:
        return "CANCEL_SELL"
    return "CANCEL"


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


class UpdateProtectionSchema(BaseModel):
    pos_side: Literal["LONG", "SHORT"] = Field(description="管理该方向的整个仓位及系统待成交计划")
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="新的止损触发价；省略则保留")
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="新的止盈触发价；省略则保留")
    reason: str = Field(description="调整原因及策略失效条件")


class UpdateStrategyProtectionSchema(BaseModel):
    order_id: str = Field(description="模拟仓位或未成交模拟订单ID")
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(description="修改理由及新的策略失效条件")

class CancelRealSchema(BaseModel):
    order_id: str = Field(description="要撤销的单个真实订单 ID。一次工具调用只填写一个订单 ID；多个订单请分别调用多次。")
    reason: str = Field(description="撤单原因，必须说明为什么撤销该订单。")

class OpenStrategySchema(BaseModel):
    orders: List[OpenOrderStrategy] = Field(description="模拟开仓指令列表")

class CancelStrategySchema(BaseModel):
    order_id: str = Field(description="要撤销的单个模拟订单 ID。一次工具调用只填写一个订单 ID；多个订单请分别调用多次。")
    reason: str = Field(description="撤单原因，必须说明为什么撤销该订单。")

class CloseStrategySchema(BaseModel):
    orders: List[CloseOrder] = Field(description="平仓模拟挂单指令列表")

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
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    try:
        orders = _normalize_order_models(orders, OpenOrderSpotDCA)
    except Exception as exc:
        return f"❌ [Error] 现货开仓参数无效: {exc}"

    for op in orders:
        try:
            action, price = op.action, op.entry_price
            latest = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在。")
                continue
            res = market_tool.place_real_order(symbol, action, op.model_dump(), agent_name=config_id)
            if res and 'id' in res:
                # 优化日志展示：增加金额和数量
                cost = price * op.amount
                enhanced_reason = f"💰 定投下单: {op.amount} {symbol.split('/')[0]} @ {price} (总额: ${cost:.2f}) | {op.reason}"
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy', price, 0, 0, enhanced_reason, trade_mode="SPOT_DCA", config_id=config_id, amount=op.amount, event_type="ORDER_CREATED")
                execution_results.append(f"✅ [下单成功] {action} {symbol} @ {price}")
            else:
                execution_results.append(f"❌ [下单失败] 交易所未返回有效订单 ID")
        except Exception as e:
            execution_results.append(f"❌ [Error] 现货开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=OpenRealSchema)
def open_position_real(orders: List[OpenOrderReal], config_id: str, symbol: str):
    """【实盘开仓并预设TP/SL】必填止盈止损，成交后系统自动挂条件市价保护单，覆盖同方向整个仓位。"""
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    try:
        orders = _normalize_order_models(orders, OpenOrderReal)
    except Exception as exc:
        return f"❌ [Error] 开仓参数无效: {exc}"

    for op in orders:
        try:
            action, price = op.action, op.entry_price
            latest = market_tool.get_account_status(symbol, is_real=True, agent_name=config_id)
            if _is_duplicate_real_order(action, price, latest.get('real_open_orders', [])):
                execution_results.append(f"⚠️ [Duplicate] {action} @ {price} 已存在。")
                continue
            from backend.utils.position_protection import PositionProtection
            res = PositionProtection(market_tool).open(symbol, op)
            if res and 'id' in res:
                # 优化日志展示：增加金额和数量
                cost = price * op.amount
                side_str = "多" if "BUY" in action else "空"
                enhanced_reason = f"🚀 实盘开{side_str}: {op.amount} {symbol.split('/')[0]} @ {price} (价值: ${cost:.2f}) | {op.reason}"
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', price, op.take_profit, op.stop_loss, enhanced_reason, trade_mode="REAL", config_id=config_id, amount=op.amount, event_type="ORDER_CREATED")
                execution_results.append(f"✅ [入场委托已提交，非成交确认] {action} {symbol} @ {price} | ID: {res['id']} | TP={op.take_profit} SL={op.stop_loss} | 保护状态={res.get('protection_state')} | 异常={res.get('protection_error') or '无'}")
            else:
                execution_results.append(f"❌ [下单失败] 交易所未返回有效订单 ID")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CloseRealSchema)
def close_position_real(orders: List[CloseOrder], config_id: str, symbol: str):
    """【平仓：挂单平掉现有持仓】。"""
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    try:
        orders = _normalize_order_models(orders, CloseOrder)
    except Exception as exc:
        return f"❌ [Error] 平仓参数无效: {exc}"

    for op in orders:
        try:
            res = market_tool.place_real_order(symbol, 'CLOSE', op.model_dump(), agent_name=config_id)
            
            if isinstance(res, dict) and res.get('status') == 'no_position':
                execution_results.append(f"⚠️ [跳过] {op.pos_side} 无持仓，无需平仓。")
                continue

            # 提取订单 ID
            order_ids = []
            if isinstance(res, dict):
                if 'id' in res:
                    order_ids.append(str(res['id']))
                elif 'orders' in res:
                    order_ids.extend([str(o['id']) for o in res['orders'] if 'id' in o])
            
            if not order_ids:
                execution_results.append(f"❌ [平仓失败] 无法获取订单 ID，请检查持仓状态。")
                continue

            # 默认取第一个 ID 作为记录
            final_log_id = order_ids[0]

            # 优化日志展示
            cost = op.entry_price * op.amount
            side_str = "多" if op.pos_side == "LONG" else "空"
            enhanced_reason = f"🏁 平{side_str}: {op.amount} {symbol.split('/')[0]} @ {op.entry_price} (价值: ${cost:.2f}) | {op.reason}"
            
            database.save_order_log(final_log_id, symbol, agent_name, f"CLOSE_{op.pos_side}", op.entry_price, 0, 0, enhanced_reason, trade_mode="REAL", config_id=config_id, amount=op.amount, status="OPEN", event_type="CLOSE_ORDER_CREATED")
            # Position closure and realized PnL must come from fill synchronization,
            # never from a successful request to place a limit/stop order.
            execution_results.append(f"✅ 下单成功 ({op.pos_side}) @ {op.entry_price} | ID: {final_log_id}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 下单失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CancelRealSchema)
def cancel_orders_real(order_id: str, reason: str, config_id: str, symbol: str):
    """【撤单：撤销一个现有真实挂单】每次只传一个 order_id。"""
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    oid = str(order_id or "").strip()
    cancel_reason = str(reason or DEFAULT_CANCEL_REASON).strip() or DEFAULT_CANCEL_REASON
    try:
        latest_row = None
        base_row = None
        with database.get_db_conn() as _conn:
            latest_row = _conn.execute(
                "SELECT side, status FROM orders WHERE order_id = ? AND config_id = ? ORDER BY id DESC LIMIT 1", (oid, config_id)
            ).fetchone()
            base_row = _conn.execute(
                "SELECT side FROM orders WHERE order_id = ? AND config_id = ? AND LOWER(side) NOT LIKE 'cancel%' ORDER BY id DESC LIMIT 1",
                (oid, config_id),
            ).fetchone()
        latest_side = str((latest_row["side"] if latest_row else "") or "").upper()
        latest_status = str((latest_row["status"] if latest_row else "") or "").upper()
        if latest_row and ("CANCEL" in latest_side or latest_status == "CANCELLED"):
            execution_results.append(f"⚠️ [Skip] 订单 {oid} 当前状态为 {latest_status or latest_side}，无需重复撤单。")
            return "\n".join(execution_results)

        orig_side = _cancel_side_from_value(base_row["side"] if base_row else (latest_row["side"] if latest_row else None))
        market_tool.place_real_order(symbol, 'CANCEL', {"cancel_order_id": oid}, agent_name=config_id)
        with database.get_db_conn() as _conn:
            _conn.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = ? AND status = 'OPEN' AND COALESCE(event_type, 'ORDER_CREATED') = 'ORDER_CREATED'", (oid,))
            _conn.commit()
        database.save_order_log(oid, symbol, agent_name, orig_side, 0, 0, 0, f"撤单成功: {oid} | {cancel_reason}", trade_mode="REAL", config_id=config_id, status="CANCELLED", event_type="CANCELLED")
        execution_results.append(f"✅ [Cancelled Real] 订单 {oid} 已撤回。")
    except Exception as e:
        execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
    return "\n".join(execution_results)


@tool(args_schema=UpdateProtectionSchema)
def update_position_protection_real(pos_side: str, reason: str, config_id: str, symbol: str,
                                    stop_loss: Optional[float] = None, take_profit: Optional[float] = None):
    """【调整实盘TP/SL】管理同方向整个仓位或已托管待成交计划；省略的价格保留。首次设置须同时给TP和SL。"""
    if stop_loss is None and take_profit is None:
        return "❌ 至少提供一个要调整的止盈或止损价格"
    from backend.utils.position_protection import PositionProtection
    try:
        plan = PositionProtection(MarketTool(config_id=config_id)).adjust(symbol, pos_side, stop_loss, take_profit)
        database.save_order_log(
            f"protection:{uuid.uuid4().hex}", symbol, config_id, pos_side,
            0, plan['take_profit'], plan['stop_loss'], reason,
            trade_mode="REAL", config_id=config_id, event_type="PROTECTION_UPDATED",
        )
        return f"保护计划已更新：{pos_side} TP={plan['take_profit']} SL={plan['stop_loss']} 状态={plan['state']}；仅ACTIVE表示本轮已核验保护单。"
    except Exception as exc:
        return f"❌ 保护调整未确认完成：{exc}；系统保留已保存计划并继续核对，不能声称已生效。"


@tool(args_schema=UpdateStrategyProtectionSchema)
def update_position_protection_strategy(order_id: str, reason: str, config_id: str, symbol: str,
                                        stop_loss: Optional[float] = None, take_profit: Optional[float] = None):
    """【调整模拟TP/SL】按订单ID更新待成交或已成交模拟仓位；未指定的价格不变。"""
    if stop_loss is None and take_profit is None:
        return '❌ 至少提供止盈或止损价格'
    from backend.utils.position_protection import PositionProtection
    try:
        with database.get_db_conn() as conn:
            row = conn.execute("SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=? AND status='OPEN'",
                               (order_id, config_id, symbol)).fetchone()
        if not row:
            return '❌ 未找到该配置的活跃模拟订单'
        reference = float(row['price'])
        if row['is_filled']:
            reference = float(MarketTool(config_id=config_id).exchange.fetch_ticker(symbol)['last'])
        sl = stop_loss if stop_loss is not None else row['stop_loss']
        tp = take_profit if take_profit is not None else row['take_profit']
        side = 'LONG' if 'BUY' in str(row['side']).upper() else 'SHORT'
        PositionProtection._validate(side, reference, sl, tp)
        with database.get_db_conn() as conn:
            changed = conn.execute(
                "UPDATE mock_orders SET stop_loss=?,take_profit=? WHERE order_id=? AND config_id=? AND status='OPEN' AND is_filled=? AND stop_loss IS ? AND take_profit IS ?",
                (sl, tp, order_id, config_id, row['is_filled'], row['stop_loss'], row['take_profit']),
            ).rowcount
            conn.commit()
        if not changed:
            return '❌ 订单状态已变化，请重新读取后再管理'
        database.save_order_log(f'protection:{uuid.uuid4().hex}', symbol, config_id, side, reference, tp, sl,
                                reason, trade_mode='STRATEGY', config_id=config_id, event_type='PROTECTION_UPDATED', parent_order_id=order_id)
        return f'模拟保护已更新：{order_id} TP={tp} SL={sl}'
    except Exception as exc:
        return f'❌ 模拟保护更新失败：{exc}'

@tool(args_schema=OpenStrategySchema)
def open_position_strategy(orders: List[OpenOrderStrategy], config_id: str, symbol: str):
    """【策略开仓：记录模拟交易】。"""
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []
    latest = market_tool.get_account_status(symbol, is_real=False, agent_name=config_id, config_id=config_id)
    remaining_available = float(latest.get('available_balance', 0) or 0)

    try:
        orders = _normalize_order_models(orders, OpenOrderStrategy)
    except Exception as exc:
        return f"❌ [Error] 策略开仓参数无效: {exc}"

    for op in orders:
        try:
            action, price = op.action, op.entry_price
            
            sl = op.stop_loss
            tp = op.take_profit

            # --- Balance & Stacking Checks ---
            order_value = price * op.amount
            if order_value > remaining_available:
                execution_results.append(
                    f"⚠️ [Insufficient Strategy Balance] 订单价值 ${order_value:.2f} 超过可用余额 ${remaining_available:.2f}。"
                )
                continue

            # 2. 检查是否重复叠加 (Stacking)
            mock_open_orders = latest.get('mock_open_orders', [])
            
            # 如果已经有同方向的单子且价格接近，视为重复
            if _is_duplicate_real_order(action, price, mock_open_orders):
                execution_results.append(f"⚠️ [Duplicate Strategy] {action} @ {price} 已存在。")
                continue
            
            # 如果已有同方向持仓，且数量已经很大，禁止叠加
            side_str = 'BUY' if 'BUY' in action else 'SELL'
            exist_same_side = [o for o in mock_open_orders if side_str in o.get('side', '').upper()]
            if len(exist_same_side) >= 1:
                # 提示已有单子，建议先撤回或等待
                execution_results.append(f"⚠️ [Stacking Blocked] {symbol} 已有 {side_str} 挂单/持仓，禁止盲目叠加。")
                continue
            # ---------------------------------------------

            expire_at = (datetime.now() + timedelta(hours=op.valid_duration_hours)).timestamp()
            mock_id = f"ST-{uuid.uuid4().hex[:6]}"
            database.create_mock_order(symbol, 'BUY' if 'BUY' in action else 'SELL', price, op.amount, sl, tp, agent_name=agent_name, config_id=config_id, order_id=mock_id, expire_at=expire_at)
            database.save_order_log(mock_id, symbol, agent_name, 'BUY' if 'BUY' in action else 'SELL', price, tp, sl, f"[Strategy] {op.reason}", trade_mode="STRATEGY", config_id=config_id, amount=op.amount, event_type="ORDER_CREATED")
            latest.setdefault('mock_open_orders', []).append({
                'order_id': mock_id,
                'side': 'BUY' if 'BUY' in action else 'SELL',
                'price': price,
                'amount': op.amount,
                'is_filled': 0,
            })
            remaining_available = max(remaining_available - order_value, 0.0)
            execution_results.append(f"✅ [Executed Strategy] {action} {symbol} @ {price} | Val: ${order_value:.2f}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CancelStrategySchema)
def cancel_orders_strategy(order_id: str, reason: str, config_id: str, symbol: str):
    """【策略撤单：撤销一个模拟挂单】每次只传一个 order_id。"""
    agent_name = config_id
    execution_results = []
    oid = str(order_id or "").strip()
    cancel_reason = str(reason or DEFAULT_CANCEL_REASON).strip() or DEFAULT_CANCEL_REASON
    try:
        with database.get_db_conn() as _conn:
            mock_row = _conn.execute(
                "SELECT side FROM mock_orders WHERE order_id = ? LIMIT 1", (oid,)
            ).fetchone()
            latest_row = _conn.execute(
                "SELECT side, status FROM orders WHERE order_id = ? AND config_id = ? ORDER BY id DESC LIMIT 1",
                (oid, config_id),
            ).fetchone()
        latest_side = str((latest_row["side"] if latest_row else "") or "").upper()
        latest_status = str((latest_row["status"] if latest_row else "") or "").upper()
        if not mock_row:
            if latest_row and ("CANCEL" in latest_side or latest_status in {"CANCELLED", "CLOSED", "FILLED"}):
                execution_results.append(f"⚠️ [Skip] 订单 {oid} 当前状态为 {latest_status or latest_side}，无需重复撤单。")
            else:
                execution_results.append(f"⚠️ [Skip] 未找到可撤销的策略挂单 {oid}。")
            return "\n".join(execution_results)

        orig_side = _cancel_side_from_value(mock_row["side"] if mock_row else None)
        cancelled = database.cancel_mock_order(oid)
        if not cancelled:
            execution_results.append(f"⚠️ [Skip] 订单 {oid} 已被其他流程撤回，跳过重复撤单。")
            return "\n".join(execution_results)

        database.save_order_log(oid, symbol, agent_name, orig_side, 0, 0, 0, f"[Strategy] 撤单成功: {oid} | {cancel_reason}", trade_mode="STRATEGY", config_id=config_id, status="CANCELLED", event_type="CANCELLED")
        execution_results.append(f"✅ [Cancelled Strategy] 订单 {oid} 已撤回。")
    except Exception as e:
        execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
    return "\n".join(execution_results)

@tool(args_schema=CloseStrategySchema)
def close_position_strategy(orders: List[CloseOrder], config_id: str, symbol: str):
    """【策略平仓：模拟平掉已有持仓】。"""
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []
    
    # 获取当前的模拟持仓
    latest = market_tool.get_account_status(symbol, is_real=False, agent_name=config_id)
    open_mock_orders = latest.get('mock_open_orders', [])

    # 获取当前市场价格以计算真实平仓盈亏
    current_price = 0
    try:
        ticker = market_tool.exchange.fetch_ticker(symbol)
        current_price = float(ticker.get('last', 0))
    except Exception as e:
        execution_results.append(f"❌ [Error] 无法获取 {symbol} 当前价格进行平仓计算: {str(e)}")
        return "\n".join(execution_results)

    try:
        orders = _normalize_order_models(orders, CloseOrder)
    except Exception as exc:
        return f"❌ [Error] 策略平仓参数无效: {exc}"

    for op in orders:
        try:
            # 用户传的是你要平的仓位方向，例如平多(LONG)，那意味着找到我们做多的单子(BUY)
            target_side = "BUY" if op.pos_side == "LONG" else "SELL"
            
            # 找到对应的模拟单
            matched_orders = [o for o in open_mock_orders if target_side in o.get('side', '').upper()]
            if not matched_orders:
                execution_results.append(f"⚠️ [跳过] 没有找到对应 {op.pos_side} 的模拟持仓可平。")
                continue
                
            for matched in matched_orders:
                order_id = matched.get('order_id')
                entry_price = float(matched.get('price', 0))
                amount = float(matched.get('amount', 0))
                
                # 使用真实当前市价计算盈亏，禁止 LLM 自定平仓价
                if target_side == "BUY": # 做多
                    pnl = (current_price - entry_price) * amount
                else: # 做空
                    pnl = (entry_price - current_price) * amount
                    
                database.close_mock_order(order_id, close_price=current_price, realized_pnl=pnl)
                database.save_order_log(order_id, symbol, agent_name, "CLOSE", current_price, 0, 0, f"[Strategy Close] 市价平仓, 盈亏: {pnl:.2f} | {op.reason}", trade_mode="STRATEGY", config_id=config_id, amount=amount, status="CLOSED", event_type="MANUAL_CLOSE", parent_order_id=order_id, realized_pnl=pnl)
                execution_results.append(f"✅ [Closed Strategy] {op.pos_side} 仓位已平，订单: {order_id}，模拟盈亏: {pnl:.2f}")
        except Exception as e:
            execution_results.append(f"❌ [Error] 策略平仓失败: {str(e)}")
    return "\n".join(execution_results)

# ==========================================
# 2. 专用分析工具
# ==========================================

@tool(args_schema=AnalyzeEventContractSchema)
def analyze_event_contract(*args, **kwargs) -> str:
    """
    [事件合约分析工具] 一次性扫描并返回当前交易对在 30min, 1h, 1d 三个关键时间窗口的客观指标看板。
    只有用户要求进行“事件合约”的交易时候才可以调用！
    使用说明：
    1. 此工具【不需要任何参数】。直接调用 `analyze_event_contract()` 即可。
    2. 它会自动处理当前正在讨论的交易对（Symbol）。
    3. 输出包含：趋势状态、VWAP 偏离、RSI、布林带宽度、成交量状态、筹码分布(POC)及支撑压力位。
    4. 适用于：当你需要快速了解多周期市场概况，或用户询问“现在行情如何”、“给我一些指标数据”时。
    """
    from backend.utils.market_data import MarketTool
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
    只有用户要求进行“事件合约”的交易时候才可以调用！
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
