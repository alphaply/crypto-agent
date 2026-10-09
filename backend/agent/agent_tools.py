import json
import math
import uuid
import time
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from backend.agent.agent_models import AdoptPositionRealSchema, OpenOrderReal, OpenOrderSpotDCA, OpenOrderStrategy, CloseOrder
import backend.database as database
from backend.utils.market_data import MarketTool
from backend.utils.logger import setup_logger
from backend.utils.spot_config_guard import serialized_spot_execution

logger = setup_logger("AgentTools")
DEFAULT_CANCEL_REASON = "未提供撤单原因"


def _exit_mode(config_id):
    from backend.config import config
    from backend.utils.exit_policy import effective_exit_mode
    return effective_exit_mode(config.get_config_by_id(config_id))


def _independent_service(config_id, symbol):
    from backend.config import config
    if (config.get_config_by_id(config_id) or {}).get('mode', 'STRATEGY').upper() == 'REAL':
        from backend.utils.independent_exits import IndependentExits
        return IndependentExits(MarketTool(config_id=config_id)), True
    from backend.database_independent import MockIndependentTrading
    return MockIndependentTrading(config_id, symbol), False


def _independent_call(config_id, symbol, action, **kwargs):
    from backend.utils.trade_operations import current_operation_id
    try:
        service, real = _independent_service(config_id, symbol)
        if not real and action == 'amend_exit':
            kwargs['current_price'] = float(MarketTool(config_id=config_id).exchange.fetch_ticker(symbol)['last'])
        kwargs['operation_id'] = current_operation_id.get()
        method = getattr(service, action)
        result = method(symbol, **kwargs) if real else method(**kwargs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except ValueError as exc:
        from backend.utils.independent_exits import IndependentPositionError
        result = {'status': 'failed', 'error': str(exc)}
        if isinstance(exc, IndependentPositionError):
            result.update(error_code=exc.code, details=exc.details)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'status': 'unknown', 'error': str(exc)}, ensure_ascii=False)


def _independent_orders(orders, config_id, symbol, opening=False):
    from backend.config import config
    from backend.utils.exit_policy import validate_entry_protection
    from backend.utils.trade_operations import current_operation_id, tool_result_status
    model = OpenOrderStrategy if opening else CloseOrder
    try:
        ops = _normalize_order_models(orders, model)
        if opening:
            for op in ops:
                validate_entry_protection(config.get_config_by_id(config_id) or {}, op)
    except Exception as exc:
        return json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False)
    results = []
    parent_id = current_operation_id.get() or uuid.uuid4().hex
    for index, op in enumerate(ops):
        token = current_operation_id.set(f'{parent_id}:{index}')
        try:
            if opening:
                result = _independent_call(config_id, symbol, 'open', op=op)
            else:
                current_price = float(MarketTool(config_id=config_id).exchange.fetch_ticker(symbol)['last'])
                exit_type = op.exit_type
                price, trigger = op.price, op.trigger_price
                if exit_type is None:
                    if op.entry_price == 0:
                        exit_type = 'market'
                    elif (op.pos_side == 'LONG' and op.entry_price < current_price) or (op.pos_side == 'SHORT' and op.entry_price > current_price):
                        exit_type, trigger = 'stop_market', op.entry_price
                    else:
                        exit_type, price = 'take_profit_limit', op.entry_price
                args = dict(pos_side=op.pos_side, amount=op.amount, exit_type=exit_type, price=price, trigger_price=trigger, reason=op.reason)
                if (config.get_config_by_id(config_id) or {}).get('mode', 'STRATEGY').upper() != 'REAL':
                    args['current_price'] = current_price
                result = _independent_call(config_id, symbol, 'close', **args)
            item = json.loads(result)
            item['index'] = index
            results.append(item)
            if tool_result_status(result) in {'failed', 'unknown'}:
                break
        except Exception as exc:
            results.append({'status': 'unknown', 'error': str(exc), 'index': index})
            break
        finally:
            current_operation_id.reset(token)
    blocked_by_index = len(results) - 1
    results.extend({'status': 'not_executed', 'index': index, 'blocked_by_index': blocked_by_index}
                   for index in range(len(results), len(ops)))
    status = tool_result_status(results)
    return json.dumps({'status': status, 'results': results}, ensure_ascii=False)


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
    symbol: Optional[str] = Field(None, description="订单未填写 symbol 时使用的配置内现货标的")

class CloseRealSchema(BaseModel):
    orders: List[CloseOrder] = Field(description="平仓指令列表")


class UpdateProtectionSchema(BaseModel):
    pos_side: Literal["LONG", "SHORT"] = Field(description="管理该方向的整个仓位及系统待成交计划")
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="新的止损触发价；省略则保留")
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="新的止盈触发价；省略则保留")
    reason: str = Field(description="调整原因及策略失效条件")


class UpdateEntrySchema(BaseModel):
    order_id: str = Field(min_length=1, description="本配置托管的未完全成交合约限价入场订单ID")
    entry_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    amount: Optional[float] = Field(None, gt=0, allow_inf_nan=False,
                                   description="修改后的总标的币数量，包含已成交部分，不是剩余数量")
    reason: str = Field(min_length=1, description="新的入场条件及改单理由；不能仅因价格移动而追单")
    pos_side: Optional[Literal["LONG", "SHORT"]] = Field(None, description="预期持仓方向：LONG开多、SHORT开空；传入后必须与托管订单一致，不会改变方向")


class UpdateStrategyEntrySchema(BaseModel):
    order_id: str = Field(min_length=1, description="本配置尚未成交的模拟入场订单ID")
    pos_side: Literal["LONG", "SHORT"] = Field(description="LONG对应BUY开多，SHORT对应SELL开空；必须与原订单一致")
    entry_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    amount: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, description="入场或保护条件变化的理由")


class UpdateStrategyProtectionSchema(BaseModel):
    order_id: str = Field(description="模拟仓位或未成交模拟订单ID")
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(description="修改理由及新的策略失效条件")

class CancelRealSchema(BaseModel):
    order_id: str = Field(description="要撤销的单个真实订单 ID。一次工具调用只填写一个订单 ID；多个订单请分别调用多次。")
    reason: str = Field(description="撤单原因，必须说明为什么撤销该订单。")

class CancelSpotSchema(CancelRealSchema):
    symbol: Optional[str] = Field(None, description="现货撤单必须选择该订单所属的配置内交易对；合约标的由系统固定")

class OpenStrategySchema(BaseModel):
    orders: List[OpenOrderStrategy] = Field(description="模拟开仓指令列表")

class CancelStrategySchema(BaseModel):
    order_id: str = Field(description="要撤销的单个模拟订单 ID。一次工具调用只填写一个订单 ID；多个订单请分别调用多次。")
    reason: str = Field(description="撤单原因，必须说明为什么撤销该订单。")

class CloseStrategySchema(BaseModel):
    orders: List[CloseOrder] = Field(description="平仓模拟挂单指令列表")


class UpdateExitSchema(BaseModel):
    order_id: str = Field(min_length=1)
    amount: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    trigger_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)


@tool(args_schema=UpdateExitSchema)
def update_exit_order(order_id: str, reason: str, config_id: str, symbol: str,
                      amount: Optional[float] = None, price: Optional[float] = None,
                      trigger_price: Optional[float] = None):
    """修改独立退出单的剩余标的币数量或价格；省略字段保留。只管理本任务当前仓位周期的订单，返回实际修改结果；待核验时不得重复提交。"""
    if _exit_mode(config_id) != 'independent_exits':
        return '❌ 仅独立退出模式支持退出改单'
    if all(v is None for v in (amount, price, trigger_price)):
        return '❌ 至少提供一个修改字段'
    return _independent_call(config_id, symbol, 'amend_exit', order_id=order_id, amount=amount,
                             price=price, trigger_price=trigger_price, reason=reason)

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
@serialized_spot_execution
def open_position_spot_dca(orders: List[OpenOrderSpotDCA], config_id: str, symbol: str):
    """【多标的现货限价买入】仅支持BUY_LIMIT，每笔可指定配置内symbol，amount为该币数量。
    所有订单和标的共享任务本轮预算及总预算，不能给每个币重复分配完整预算。
    有执行决定时调用，等待无需调用；用户要求保持的挂单不得改动。
    返回委托不等于成交，接口失败如实报告，不把文字计划当作已执行，不重复提交未知结果。
    """
    import ccxt
    from backend.config import config as global_config
    from backend.utils.spot_execution import (resolve_spot_symbol, reserve_spot_batch, finish_spot_reservation,
                                               reconcile_spot_reservations, spot_client_order_id)
    from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status

    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    parent_id = current_operation_id.get() or uuid.uuid4().hex
    try:
        if agent_config.get('mode', '').upper() != 'SPOT_DCA':
            raise ValueError('现货买入仅允许 SPOT_DCA 任务')
        orders = _normalize_order_models(orders, OpenOrderSpotDCA)
        if not orders:
            raise ValueError('orders 不能为空')
        default_symbol = resolve_spot_symbol(agent_config, symbol)
        # Validate the complete batch before constructing a network-capable client.
        orders = [op.model_copy(update={'symbol': resolve_spot_symbol(agent_config, op.symbol or default_symbol)})
                  for op in orders]
        reconcile_spot_reservations(config_id, agent_config, lambda: MarketTool(config_id=config_id))
        reserve_spot_batch(config_id, agent_config, parent_id, orders)
    except Exception as exc:
        return json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False)

    try:
        market_tool = MarketTool(config_id=config_id)
        account = market_tool.get_account_status(orders[0].symbol, is_real=True, agent_name=config_id)
        if account.get('error'):
            raise ValueError(f"无法核验现货账户: {account['error']}")
        available = float(account.get('available_balance'))
        requested = sum(op.entry_price * op.amount for op in orders)
        if not math.isfinite(available) or available < requested:
            raise ValueError(f'现货可用计价币余额不足：可用 {available:g}，组合订单需要 {requested:g}')
    except Exception as exc:
        for index in range(len(orders)):
            finish_spot_reservation(config_id, f'{parent_id}:{index}', 'released')
        return json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False)

    results = []
    stopped = False
    for index, op in enumerate(orders):
        child_id = f'{parent_id}:{index}'
        if stopped:
            finish_spot_reservation(config_id, child_id, 'released')
            results.append({'index': index, 'symbol': op.symbol, 'status': 'not_executed'})
            continue

        def submit():
            writing = False
            try:
                latest = market_tool.get_account_status(op.symbol, is_real=True, agent_name=config_id)
                if latest.get('error'):
                    raise ValueError(f"无法核验现货账户: {latest['error']}")
                if _is_duplicate_real_order(op.action, op.entry_price, latest.get('real_open_orders', [])):
                    finish_spot_reservation(config_id, child_id, 'released')
                    return {'status': 'skipped', 'reason': '该标的同价位已有买入挂单'}
                available = float(latest.get('available_balance'))
                cost = op.entry_price * op.amount
                if not math.isfinite(available) or available < cost:
                    raise ValueError(f'现货可用计价币余额不足：可用 {available:g}，订单需要 {cost:g}')
                writing = True
                params = {**op.model_dump(), 'client_order_id': spot_client_order_id(config_id, child_id)}
                response = market_tool.place_real_order(op.symbol, op.action, params, agent_name=config_id)
                if not response or not response.get('id'):
                    raise RuntimeError('交易所未返回有效订单 ID，下单结果待核验')
                order_id = str(response['id'])
                finish_spot_reservation(config_id, child_id, 'submitted', order_id)
                cost = op.entry_price * op.amount
                reason = f"💰 定投下单: {op.amount} {op.symbol.split('/')[0]} @ {op.entry_price} (金额: {cost:.2f}) | {op.reason}"
                database.save_order_log(order_id, op.symbol, agent_name, 'buy', op.entry_price, 0, 0,
                                        reason, trade_mode='SPOT_DCA', config_id=config_id,
                                        amount=op.amount, event_type='ORDER_CREATED')
                return {'status': 'submitted', 'order_id': order_id, 'symbol': op.symbol,
                        'amount': op.amount, 'entry_price': op.entry_price,
                        'message': '现货委托已提交，非成交确认'}
            except Exception as exc:
                # Network and unexpected exceptions after a write may hide a
                # successful order; keep its reservation and stop the batch.
                definite = not writing or isinstance(exc, (ccxt.InsufficientFunds, ccxt.InvalidOrder,
                                                           ccxt.AuthenticationError, ccxt.PermissionDenied))
                status = 'failed' if definite else 'unknown'
                finish_spot_reservation(config_id, child_id, 'released' if definite else 'unknown')
                return {'status': status, 'symbol': op.symbol, 'error': str(exc)}

        try:
            result = run_once(config_id, op.symbol, child_id, op.model_dump(), submit)
        except Exception as exc:
            finish_spot_reservation(config_id, child_id, 'unknown')
            result = {'status': 'unknown', 'symbol': op.symbol, 'error': str(exc)}
        results.append({'index': index, **result})
        stopped = tool_result_status(result) in {'failed', 'unknown'}
    return json.dumps({'status': tool_result_status(results), 'results': results}, ensure_ascii=False)

@tool(args_schema=OpenRealSchema)
def open_position_real(orders: List[OpenOrderReal], config_id: str, symbol: str):
    """【实盘合约限价开仓】有执行决定时调用，等待无需调用；接口失败如实报告，不把计划当作已执行。
    BUY_LIMIT开多LONG，SELL_LIMIT开空SHORT；必填amount（标的币数量）、entry_price、reason。
    TP/SL要求由退出模式决定：attached_required必须同时提供TP和SL，attached_optional选填，independent_exits禁止附带TP/SL。
    仅附带保护模式：首次开仓省略的保护不会创建。同方向加仓省略TP/SL则继承现有计划；填写则先更新同方向整个仓位的对应保护，另一项保留，非单笔独立保护。
    加仓不自动按均价移动保护，加仓失败不回滚已更新的保护。不得把浮亏加仓/扩大止损作为默认解套手段。
    返回入场委托不等于成交；仅attached_required/attached_optional由后台维护任务在成交后安装已配置的交易所条件市价TP/SL（含部分成交），存在轮询及网络延迟，并非原子绑定。
    independent_exits不会自动安装TP/SL；Agent须在确认成交后调用close_position_real创建独立退出单，分别指定价格和数量。
    省略TP/SL时须核对已有保护，明确未保护风险与退出条件。
    WAITING为待成交；ACTIVE且error为空仅代表最近核验通过；EXITING为退出清理未完成。不得声称未核验保护已生效。
    用户要求保持的挂单不得改动；失败/结果未知时停止并核对，不重复开仓。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_orders(orders, config_id, symbol, opening=True)
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    try:
        orders = _normalize_order_models(orders, OpenOrderReal)
        from backend.utils.exit_policy import validate_entry_protection
        for order in orders:
            validate_entry_protection(agent_config, order)
    except Exception as exc:
        return f"❌ [Error] 开仓参数无效: {exc}"

    for op in orders:
        try:
            action, price = op.action, op.entry_price
            from backend.utils.position_protection import PositionProtection
            res = PositionProtection(market_tool).open(symbol, op)
            if res and 'id' in res:
                # 优化日志展示：增加金额和数量
                cost = price * op.amount
                side_str = "多" if "BUY" in action else "空"
                enhanced_reason = f"🚀 实盘开{side_str}: {op.amount} {symbol.split('/')[0]} @ {price} (价值: ${cost:.2f}) | {op.reason}"
                database.save_order_log(str(res['id']), symbol, agent_name, 'buy' if 'BUY' in action else 'sell', price, res.get('take_profit') or 0, res.get('stop_loss') or 0, enhanced_reason, trade_mode="REAL", config_id=config_id, amount=op.amount, event_type="ORDER_CREATED")
                protection = f"TP={res.get('take_profit') or '未设置'} SL={res.get('stop_loss') or '未设置'}（同方向整个仓位）"
                execution_results.append(f"✅ [入场委托已提交，非成交确认] {action} {symbol} @ {price} | ID: {res['id']} | {protection} | 计划状态={res.get('protection_state')} | 异常={res.get('protection_error') or '无'}")
            else:
                execution_results.append(f"❌ [下单失败] 交易所未返回有效订单 ID")
        except Exception as e:
            execution_results.append(f"❌ [Error] 开仓失败: {str(e)}")
            break
    return "\n".join(execution_results)

@tool(args_schema=AdoptPositionRealSchema)
def adopt_position_real(pos_side: str, expected_amount: float, reason: str, config_id: str, symbol: str):
    """【显式接管已有手动合约仓位】仅用于REAL实盘合约的independent_exits独立退出模式。
    用户已要求管理已有手动仓位后，先查询并核对账户、标的、LONG/SHORT方向与该方向全部标的币数量，再显式调用本工具。
    expected_amount必须是核对后的标的币数量，不是合约张数；reason说明用户意图与接管原因。不得因周期缺失而自动接管其他任务仓位。
    接管只建立本任务管理记录，不下单、不成交、不创建保护，也不改变已有仓位和挂单。成功后再按用户要求调用close_position_real挂独立SL/TP或平仓。
    接管成功不代表止损止盈已生效；返回拒绝或未知结果时停止并核对，不通过新增开仓或更换operation_id重试绕过校验。
    """
    from backend.config import config
    from backend.utils.trade_operations import current_operation_id
    try:
        params = AdoptPositionRealSchema.model_validate({
            'pos_side': pos_side, 'expected_amount': expected_amount, 'reason': reason,
        })
        cfg = config.get_config_by_id(config_id) or {}
        if str(cfg.get('mode') or '').upper() != 'REAL' or _exit_mode(config_id) != 'independent_exits':
            raise ValueError('接管已有仓位仅允许 REAL 实盘合约的独立退出模式')
        from backend.utils.position_adoption import adopt_position
        result = adopt_position(MarketTool(config_id=config_id), symbol, params.pos_side,
                                params.expected_amount, params.reason, current_operation_id.get())
        return json.dumps(result, ensure_ascii=False, default=str)
    except ValueError as exc:
        from backend.utils.independent_exits import IndependentPositionError
        result = {'status': 'failed', 'error': str(exc)}
        if isinstance(exc, IndependentPositionError):
            result.update(error_code=exc.code, details=exc.details)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'status': 'unknown', 'error': str(exc)}, ensure_ascii=False)


@tool(args_schema=CloseRealSchema)
def close_position_real(orders: List[CloseOrder], config_id: str, symbol: str):
    """【实盘部分/全部平仓】pos_side是持仓方向：LONG平多（卖出），SHORT平空（买入），不能把买卖方向当作持仓方向。
    amount为标的币数量，0解析为提交时该方向数量。显式exit_type=market立即退出，take_profit_limit配price限价止盈，stop_market配trigger_price触发市价止损。
    独立退出模式每档数量固定，加仓不扩大旧退出单；退出委托仅针对已成交仓位。旧entry_price参数仍兼容。
    订单提交不等于已成交；策略失效后不能靠等待更优价格延长风险。返回错误或未知结果需报告并核对，不把文字计划当作已执行。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_orders(orders, config_id, symbol)
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
                break

            from backend.utils.execution_ledger import account_scope, register_order
            from backend.utils.position_protection import PositionProtection
            try:
                canonical_symbol = PositionProtection(market_tool)._market(symbol)['symbol']
                for oid in order_ids:
                    register_order(account_scope(market_tool.exchange, config_id), canonical_symbol, oid,
                                   config_id, 'agent_exit', reason=op.reason, side=op.pos_side)
            except Exception as exc:
                execution_results.append(f'⚠️ 平仓委托已提交 {order_ids}，但归因记录失败：{exc}；不要重复平仓。')

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
            break
    return "\n".join(execution_results)

@serialized_spot_execution
def _cancel_spot_order(order_id: str, reason: str, config_id: str, symbol: str, agent_config: dict):
    import math
    from backend.config import config as runtime_config
    from backend.utils.spot_execution import assert_owned_spot_order, resolve_spot_symbol

    writing = False
    try:
        agent_config = runtime_config.get_config_by_id(config_id) or {}
        symbol = resolve_spot_symbol(agent_config, symbol)
        assert_owned_spot_order(None, symbol, order_id, config_id)
        market_tool = MarketTool(config_id=config_id)
        writing = True
        market_tool.place_real_order(symbol, 'CANCEL', {'cancel_order_id': str(order_id)}, agent_name=config_id)
        # Re-read fills after cancellation: a partial fill still consumes budget.
        verified = market_tool.exchange.fetch_order(str(order_id), symbol)
        status = str(verified.get('status') or '').lower()
        if status not in {'canceled', 'cancelled', 'closed', 'filled', 'expired'} or verified.get('filled') is None:
            raise ValueError('撤单或成交数量尚未核验，保留原预算占用')
        filled = float(verified['filled'])
        average = float(verified.get('average') or verified.get('price') or 0)
        cost = float(verified.get('cost') or filled * average)
        if any(not math.isfinite(value) or value < 0 for value in (filled, average, cost)) or (filled > 0 and cost <= 0):
            raise ValueError('撤单后的成交成本无效，保留原预算占用')
        local_status = 'FILLED' if status in {'closed', 'filled'} else 'CANCELLED'
        with database.get_db_conn() as conn:
            conn.execute("""UPDATE orders SET status=?,filled_amount=?,filled_cost=?,avg_fill_price=?
                WHERE order_id=? AND config_id=? AND symbol=? AND trade_mode='SPOT_DCA'
                AND COALESCE(event_type,'ORDER_CREATED')='ORDER_CREATED'""",
                         (local_status, filled, cost, average, str(order_id), config_id, symbol))
            conn.commit()
        database.upsert_spot_order_fill(str(order_id), config_id, symbol, local_status, filled, cost, average)
        if local_status == 'CANCELLED':
            database.save_order_log(str(order_id), symbol, agent_config.get('model', 'Unknown'), 'CANCEL_BUY',
                                    0, 0, 0, f'撤单成功: {order_id} | {reason}', trade_mode='SPOT_DCA',
                                    config_id=config_id, status='CANCELLED', event_type='CANCELLED')
        return json.dumps({'status': 'completed', 'order_id': str(order_id), 'symbol': symbol,
                           'exchange_status': status, 'filled_amount': filled, 'filled_cost': cost,
                           'message': '订单已成交，未撤销成交' if local_status == 'FILLED' else '撤单及成交成本已核验'}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'status': 'unknown' if writing else 'failed', 'order_id': str(order_id),
                           'symbol': symbol, 'error': str(exc)}, ensure_ascii=False)


@tool(args_schema=CancelRealSchema)
def cancel_orders_real(order_id: str, reason: str, config_id: str, symbol: str):
    """【撤销真实挂单】每次只传一个order_id及reason。用户明确要求保持的挂单不得撤销。
    不得取消保护来绕过入场/TP/SL校验，不用撤单重开绕过改单pending/未知状态。
    撤单结果以工具返回为准，失败或不确定需核对当前订单和成交，不声称已完成。
    """
    from backend.config import config as global_config
    agent_config = global_config.get_config_by_id(config_id) or {}
    if agent_config.get('mode', '').upper() == 'SPOT_DCA':
        return _cancel_spot_order(order_id, reason, config_id, symbol, agent_config)
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_call(config_id, symbol, 'cancel', order_id=order_id)
    agent_name = agent_config.get('model', 'Unknown')
    market_tool = MarketTool(config_id=config_id)
    execution_results = []

    oid = str(order_id or "").strip()
    cancel_reason = str(reason or DEFAULT_CANCEL_REASON).strip() or DEFAULT_CANCEL_REASON
    try:
        latest_row = None
        base_row = None
        from backend.utils.order_ownership import symbol_aliases
        if agent_config.get('mode', '').upper() == 'REAL':
            from backend.utils.order_ownership import assert_owned_perpetual_order
            assert_owned_perpetual_order(market_tool, symbol, oid, config_id)
        aliases = symbol_aliases(market_tool, symbol)
        placeholders = ','.join('?' for _ in aliases)
        with database.get_db_conn() as _conn:
            latest_row = _conn.execute(
                f"SELECT side, status FROM orders WHERE order_id = ? AND config_id = ? AND symbol IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (oid, config_id, *aliases)
            ).fetchone()
            base_row = _conn.execute(
                f"SELECT side FROM orders WHERE order_id = ? AND config_id = ? AND symbol IN ({placeholders}) AND LOWER(side) NOT LIKE 'cancel%' ORDER BY id DESC LIMIT 1",
                (oid, config_id, *aliases),
            ).fetchone()
        latest_side = str((latest_row["side"] if latest_row else "") or "").upper()
        latest_status = str((latest_row["status"] if latest_row else "") or "").upper()
        if latest_row and ("CANCEL" in latest_side or latest_status == "CANCELLED"):
            execution_results.append(f"⚠️ [Skip] 订单 {oid} 当前状态为 {latest_status or latest_side}，无需重复撤单。")
            return "\n".join(execution_results)

        orig_side = _cancel_side_from_value(base_row["side"] if base_row else (latest_row["side"] if latest_row else None))
        market_tool.place_real_order(symbol, 'CANCEL', {"cancel_order_id": oid}, agent_name=config_id)
        with database.get_db_conn() as _conn:
            _conn.execute(f"UPDATE orders SET status = 'CANCELLED' WHERE order_id = ? AND config_id=? AND symbol IN ({placeholders}) "
                          "AND status = 'OPEN' AND COALESCE(event_type, 'ORDER_CREATED') = 'ORDER_CREATED'",
                          (oid, config_id, *aliases))
            _conn.commit()
        database.save_order_log(oid, symbol, agent_name, orig_side, 0, 0, 0, f"撤单成功: {oid} | {cancel_reason}", trade_mode="REAL", config_id=config_id, status="CANCELLED", event_type="CANCELLED")
        execution_results.append(f"✅ [Cancelled Real] 订单 {oid} 已撤回。")
    except Exception as e:
        execution_results.append(f"❌ [Error] 撤单失败 ({oid}): {str(e)}")
    return "\n".join(execution_results)


cancel_orders_spot = cancel_orders_real.model_copy(update={'args_schema': CancelSpotSchema})


@tool(args_schema=UpdateProtectionSchema)
def update_position_protection_real(pos_side: str, reason: str, config_id: str, symbol: str,
                                    stop_loss: Optional[float] = None, take_profit: Optional[float] = None):
    """【调整实盘TP/SL】pos_side=LONG或SHORT表示持仓方向；管理同方向整个仓位及托管待成交计划，非单笔订单独立保护。
    至少提供stop_loss或take_profit及reason；省略字段保留，可首次只设置其中一个。
    待成交多单SL<入场<TP、空单TP<入场<SL（只校验已设置价格）；已有仓位按当前触发参考价校验，允许保护盈利的移动止损。
    若为入场改单先调保护，须同时兼容旧入场、新入场及现有仓位。两个调用不是原子事务，任一步失败立即停止并核对，不能取消保护绕过校验。
    优先新单确认后撤旧单；交易所拒绝并存时会核验撤旧再重建，期间存在保护空窗，异常必须报告。
    以实际返回确认结果；WAITING为等待成交；ACTIVE且error为空只表示最近核验通过，不是未来保证；EXITING表示退出清理未完成。
    用户要求保持的订单不得改动；不得把扩大止损作为默认解套手段，说明新的失效条件和风险变化。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return '❌ 独立退出模式通过 close/update_exit_order 管理退出，不使用整仓 TP/SL'
    if stop_loss is None and take_profit is None:
        return "❌ 至少提供一个要调整的止盈或止损价格"
    from backend.utils.position_protection import PositionProtection
    try:
        plan = PositionProtection(MarketTool(config_id=config_id)).adjust(symbol, pos_side, stop_loss, take_profit)
        database.save_order_log(
            f"protection:{uuid.uuid4().hex}", symbol, config_id, pos_side,
            0, plan['take_profit'] or 0, plan['stop_loss'] or 0, reason,
            trade_mode="REAL", config_id=config_id, event_type="PROTECTION_UPDATED",
        )
        return f"保护计划已更新：{pos_side} TP={plan['take_profit']} SL={plan['stop_loss']} 状态={plan['state']}；仅ACTIVE表示本轮已核验保护单。"
    except Exception as exc:
        return f"❌ 保护调整未确认完成：{exc}；系统保留已保存计划并继续核对，不能声称已生效。"


@tool(args_schema=UpdateEntrySchema)
def update_entry_order_real(order_id: str, reason: str, config_id: str, symbol: str,
                            entry_price: Optional[float] = None, amount: Optional[float] = None,
                            pos_side: Optional[str] = None):
    """【修改实盘入场价/数量】只处理本配置托管、未完全成交的合约限价入场单，不能改保护单/平仓单，不适用于现货定投。
    指定order_id、reason及entry_price和/或amount；amount为含已成交部分的总标的币数量，必须大于已成交量。省略字段保留。
    pos_side=LONG为BUY开多、SHORT为SELL开空，必须与原单一致；改单保持方向，不能翻多/翻空。用户要求保持的挂单不得改动。
    保留原TP/SL，新入场仍须满足多单SL<入场<TP、空单TP<入场<SL（仅校验已设置价格）。
    TP/SL用update_position_protection_real管理整个同方向仓位；若先调保护，须同时兼容旧入场、新入场及现有仓位。
    两个调用不是原子事务，任一步失败立即停止并核对，不能取消保护绕过校验。
    仅confirmed表示改单确认；pending不可重复提交，需核对订单与成交，不可撤单重开；unchanged表示原值未变。报告实际返回，不能把计划当作执行结果。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_call(config_id, symbol, 'amend_entry', order_id=order_id, entry_price=entry_price,
                                 amount=amount, reason=reason, pos_side=pos_side)
    from backend.config import config as global_config
    from backend.utils.position_protection import PositionProtection
    if (global_config.get_config_by_id(config_id) or {}).get('mode', '').upper() != 'REAL':
        return '❌ 仅实盘合约可使用限价改单工具'
    try:
        result = PositionProtection(MarketTool(config_id=config_id)).amend_entry(
            symbol, order_id, entry_price, amount, reason, pos_side=pos_side)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return f'❌ 改单未确认：{exc}；查询当前订单和成交，不能直接重新开单。'


@tool(args_schema=UpdateStrategyEntrySchema)
def update_entry_order_strategy(order_id: str, pos_side: str, reason: str, config_id: str, symbol: str,
                                entry_price: Optional[float] = None, stop_loss: Optional[float] = None,
                                take_profit: Optional[float] = None, amount: Optional[float] = None):
    """【修改模拟入场价及TP/SL】仅本配置尚未成交的模拟入场单；指定order_id、pos_side、reason及entry_price/stop_loss/take_profit至少一项。
    同时校验并修改入场价、数量和保护；省略字段保留，有效期不变。LONG对应BUY开多、SHORT对应SELL开空，必须与原单一致，不可翻多/翻空。
    多单SL<入场<TP、空单TP<入场<SL；用户要求保持的挂单不得改动。
    已成交仓位不可修改历史入场价，只能用update_position_protection_strategy管理保护。以返回结果为准，失败不能当作已修改。
    """
    if _exit_mode(config_id) == 'independent_exits':
        if stop_loss is not None or take_profit is not None:
            return '❌ 独立退出模式不在入场单附带 TP/SL'
        return _independent_call(config_id, symbol, 'amend_entry', order_id=order_id, entry_price=entry_price,
                                 amount=amount, reason=reason, pos_side=pos_side)
    from backend.utils.position_protection import PositionProtection
    if all(value is None for value in (entry_price, stop_loss, take_profit, amount)):
        return '❌ 至少提供入场价、止盈或止损之一'
    if not reason.strip():
        return '❌ 必须提供修改理由'
    try:
        with database.get_db_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT * FROM mock_orders WHERE order_id=? AND config_id=? AND symbol=? AND status='OPEN' AND is_filled=0",
                (order_id, config_id, symbol),
            ).fetchone()
            if not row:
                return '❌ 未找到本配置未成交模拟挂单；已成交仓位不可修改入场价'
            side = {'BUY': 'LONG', 'SELL': 'SHORT'}.get(str(row['side']).upper())
            if side is None or side != pos_side:
                return '❌ 多空方向不匹配，禁止修改订单方向'
            if row['expire_at'] is not None and float(row['expire_at']) <= time.time():
                return '❌ 订单已过期，不能改单'
            price = entry_price if entry_price is not None else float(row['price'])
            new_amount = amount if amount is not None else float(row['amount'])
            sl = stop_loss if stop_loss is not None else row['stop_loss']
            tp = take_profit if take_profit is not None else row['take_profit']
            PositionProtection._validate(side, price, sl, tp)
            # Simulation reserves full order notional. Check all open orders while holding the write lock.
            account = conn.execute('SELECT balance FROM mock_accounts WHERE config_id=?', (config_id,)).fetchone()
            reserved = conn.execute(
                "SELECT COALESCE(SUM(price*amount),0) FROM mock_orders WHERE config_id=? AND status='OPEN' AND order_id!=?",
                (config_id, order_id),
            ).fetchone()[0]
            if not account or price * new_amount > max(0, float(account['balance']) - reserved):
                return '❌ 修改后订单名义金额超过模拟可用余额'
            conn.execute('UPDATE mock_orders SET price=?,stop_loss=?,take_profit=?,amount=? WHERE order_id=? AND config_id=? AND symbol=?',
                         (price, sl, tp, new_amount, order_id, config_id, symbol))
            conn.commit()
        audit_warning = ''
        try:
            database.save_order_log(
                f'amend:{uuid.uuid4().hex}', symbol, config_id, side, price, tp or 0, sl or 0,
                f'原入场={row["price"]} TP={row["take_profit"]} SL={row["stop_loss"]}；{reason}',
                trade_mode='STRATEGY', config_id=config_id, amount=new_amount,
                event_type='ORDER_AMENDED', parent_order_id=order_id,
            )
        except Exception:
            audit_warning = '；改单已生效但审计日志写入失败，不要重复提交'
        return f'✅ 模拟改单已确认：ID={order_id} {side} Entry={price} Amount={new_amount} TP={tp} SL={sl}；有效期保持不变。理由：{reason}{audit_warning}'
    except Exception as exc:
        return f'❌ 模拟改单失败：{exc}'


@tool(args_schema=UpdateStrategyProtectionSchema)
def update_position_protection_strategy(order_id: str, reason: str, config_id: str, symbol: str,
                                        stop_loss: Optional[float] = None, take_profit: Optional[float] = None):
    """【调整模拟TP/SL】按order_id更新本配置待成交或已成交模拟仓位；至少提供stop_loss或take_profit及reason，省略字段保留。
    BUY为LONG多仓，SELL为SHORT空仓；未成交按入场价校验多单SL<入场<TP、空单TP<入场<SL，已成交按当前价校验，允许保护盈利的移动止损。
    用户要求保持的订单不得改动；报告实际返回，失败不能当作已更新。调整需说明新的失效条件和风险变化。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return '❌ 独立退出模式通过 close/update_exit_order 管理退出'
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
        side = {'BUY': 'LONG', 'SELL': 'SHORT'}.get(str(row['side']).upper())
        if side is None:
            return '❌ 无法确认模拟订单多空方向'
        PositionProtection._validate(side, reference, sl, tp)
        with database.get_db_conn() as conn:
            changed = conn.execute(
                "UPDATE mock_orders SET stop_loss=?,take_profit=? WHERE order_id=? AND config_id=? AND symbol=? AND status='OPEN' AND is_filled=? AND price=? AND stop_loss IS ? AND take_profit IS ?",
                (sl, tp, order_id, config_id, symbol, row['is_filled'], row['price'], row['stop_loss'], row['take_profit']),
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
    """【模拟合约限价开仓/加仓】BUY_LIMIT开多LONG，SELL_LIMIT开空SHORT；amount为标的币数量，需entry_price和reason。
    TP/SL要求由任务退出模式决定：attached_required必须同时提供TP和SL，attached_optional选填，independent_exits禁止附带TP/SL。
    independent_exits不会自动安装TP/SL；Agent须在确认成交后调用close_position_strategy创建独立退出单，分别指定价格和数量。
    省略TP/SL时须核对已有保护，明确未保护风险与退出条件。
    多单SL<入场<TP，空单TP<入场<SL；valid_duration_hours为挂单有效期，默认24小时。
    有执行决定时调用，等待无需调用。记录挂单不等于成交，以工具返回为准；失败如实报告，不能把文字计划当作已执行。
    用户要求保持的挂单不得改动，不盲目叠加同方向挂单/仓位。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_orders(orders, config_id, symbol, opening=True)
    agent_name = config_id
    market_tool = MarketTool(config_id=config_id)
    execution_results = []
    latest = market_tool.get_account_status(symbol, is_real=False, agent_name=config_id, config_id=config_id)
    remaining_available = float(latest.get('available_balance', 0) or 0)

    try:
        orders = _normalize_order_models(orders, OpenOrderStrategy)
        from backend.config import config
        from backend.utils.exit_policy import validate_entry_protection
        for order in orders:
            validate_entry_protection(config.get_config_by_id(config_id) or {}, order)
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
                break

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
            break
    return "\n".join(execution_results)

@tool(args_schema=CancelStrategySchema)
def cancel_orders_strategy(order_id: str, reason: str, config_id: str, symbol: str):
    """【撤销模拟挂单】每次只传一个order_id及reason；用户要求保持的挂单不得撤销。
    以返回确认撤单结果，失败/不确定需核对，不声称已完成。保护调整使用update_position_protection_strategy。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_call(config_id, symbol, 'cancel', order_id=order_id)
    agent_name = config_id
    execution_results = []
    oid = str(order_id or "").strip()
    cancel_reason = str(reason or DEFAULT_CANCEL_REASON).strip() or DEFAULT_CANCEL_REASON
    try:
        with database.get_db_conn() as _conn:
            mock_row = _conn.execute(
                "SELECT side FROM mock_orders WHERE order_id = ? AND config_id=? AND symbol=? AND is_filled=0 LIMIT 1", (oid, config_id, symbol)
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
    """【模拟平仓】pos_side为要退出的持仓方向：LONG平多（原BUY）、SHORT平空（原SELL），不能把买卖方向当作持仓方向。
    独立退出模式支持exit_type=market市价减仓、take_profit_limit配price限价止盈、stop_market配trigger_price触发止损。
    amount为标的币数量，0解析为当前已成交数量；加仓不会自动扩大已有退出数量。旧参数兼容。
    必须提供reason；以返回的平仓结果和盈亏为准，失败不能当作已平仓。
    """
    if _exit_mode(config_id) == 'independent_exits':
        return _independent_orders(orders, config_id, symbol)
    try:
        ops = _normalize_order_models(orders, CloseOrder)
        for op in ops:
            if op.exit_type not in (None, 'market') or op.entry_price > 0:
                return '❌ 附带保护的模拟模式仅支持市价减仓；条件退出单请使用独立退出模式'
        current_price = float(MarketTool(config_id=config_id).exchange.fetch_ticker(symbol)['last'])
        results = []
        for op in ops:
            result = database.reduce_mock_positions(config_id, symbol, op.pos_side, op.amount, current_price, op.reason)
            if result.get('status') == 'no_position':
                results.append(f'⚠️ [跳过] 没有找到对应 {op.pos_side} 的模拟持仓可平。')
            else:
                results.append(f"✅ [Closed Strategy] {op.pos_side} 已退出 {result['closed_amount']}，剩余 {result['remaining']}，盈亏 {result['realized_pnl']:.2f}")
        return '\n'.join(results)
    except Exception as exc:
        return f'❌ 模拟减仓失败：{exc}'

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
