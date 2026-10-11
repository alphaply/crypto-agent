"""Thin versioned action adapters; exchange and simulation own order lifecycle."""
from __future__ import annotations

import json

from langchain_core.tools import tool

from backend.agent.agent_models import OpenOrderStrategy
from backend.agent.trading_contracts import CANONICAL_MODELS, CancelInput, CloseInput, EditInput, OpenInput
from backend.utils.trade_operations import current_operation_id, tool_result_status


def describe_order(config_id: str, symbol: str, order_id: str) -> dict:
    from backend.agent.agent_tools import _independent_service
    service, real = _independent_service(config_id, symbol)
    return service.describe_order(symbol, order_id) if real else service.describe_order(order_id)


def canonical_receipt(result: dict) -> dict:
    """Keep execution evidence while separating an operation from its order state."""
    result = dict(result)
    from backend.utils.exit_orders import order_reference, parse_order_reference
    if not parse_order_reference(result.get('order_id') or '')[0] and (
            result.get('order_id') or result.get('exchange_order_id') or result.get('id')):
        result['order_id'] = order_reference(result)
    if result.get('contract_version') not in {None, 2}:
        result.setdefault('order_contract_version', result['contract_version'])
    result['contract_version'] = 2
    status = str(result.get('status') or '').lower()
    if status in {'open', 'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'waiting_trigger', 'partially_filled'}:
        result.setdefault('order_status', 'filled' if status == 'closed' else 'canceled' if status == 'cancelled' else status)
    result['status'] = tool_result_status(result)
    return result


def execute_action(action: str, arguments: dict, config_id: str, symbol: str) -> str:
    from backend.agent import agent_tools
    from backend.config import config
    from backend.utils.exit_policy import effective_exit_mode
    cfg = config.get_config_by_id(config_id) or {}
    if cfg.get('mode', 'STRATEGY').upper() not in {'REAL', 'STRATEGY'} or effective_exit_mode(cfg) != 'independent_exits':
        raise ValueError('Canonical trading actions require independent_exits perpetual mode')
    args = CANONICAL_MODELS[action].model_validate(arguments).model_dump(exclude_none=True)
    from backend.mcp.guard import preflight_tool
    if action == 'open':
        args = {'op': OpenOrderStrategy(action='BUY_LIMIT' if args['side'] == 'LONG' else 'SELL_LIMIT',
                                       entry_price=args['price'], amount=args['amount'], reason=args['reason']),
                'contract_version': 2}
    elif action == 'close':
        args['pos_side'] = args.pop('side')
        args['exit_type'] = args.pop('order_type')
        args['contract_version'] = 2
        args.setdefault('amount', None)
    if action == 'cancel':
        args.pop('reason')  # Service cancellation retains its existing ownership and lifecycle checks.
    args['operation_id'] = current_operation_id.get()
    try:
        preflight_tool(action, arguments, config_id, symbol)
        service, real = agent_tools._independent_service(config_id, symbol)
        if not real and action in {'close', 'edit'}:
            args['current_price'] = float(agent_tools.MarketTool(config_id=config_id).exchange.fetch_ticker(symbol)['last'])
        method = getattr(service, action)
        result = method(symbol, **args) if real else method(**args)
        return json.dumps(canonical_receipt(result), ensure_ascii=False, default=str)
    except ValueError as exc:
        result = {'status': 'failed', 'contract_version': 2, 'error': str(exc)}
        if hasattr(exc, 'code'):
            result.update(error_code=exc.code, details=getattr(exc, 'details', {}))
        elif 'unsupported_combination' in str(exc):
            result['error_code'] = 'unsupported_combination'
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'status': 'unknown', 'contract_version': 2, 'error': str(exc)}, ensure_ascii=False)


@tool('open', args_schema=OpenInput)
def open_trade(side: str, amount: float, price: float, reason: str, config_id: str, symbol: str):
    """独立合约限价开仓/加仓。side=LONG/SHORT，amount为基础币，price为限价，reason必填。
    可同轮多次调用分批建仓；同币多空要求Hedge Mode。无附带TP/SL；确认成交后可本轮或以后调用close。
    例：{"side":"LONG","amount":0.2,"price":100,"reason":"第一档入场"}。
    submitted仅为已提交，未知结果先核验原操作，不换ID重发。"""
    return execute_action('open', dict(side=side, amount=amount, price=price, reason=reason), config_id, symbol)


@tool('close', args_schema=CloseInput)
def close_trade(side: str, order_type: str, reason: str, config_id: str, symbol: str,
                amount: float | None = None, close_all: bool | None = None,
                price: float | None = None, trigger_price: float | None = None):
    """独立退出已成交方向：LONG平多、SHORT平空。amount>0基础币或close_all=true二选一。
    market无价格；limit用price；stop_market/take_profit_market用trigger_price；stop_limit/take_profit_limit两价都要。
    可多次分批TP/SL；固定档不自动重分配。all仅支持market与条件市价：前者提交前快照，后者触发时全平（含之后同方向加仓）。
    例：{"side":"LONG","order_type":"stop_market","trigger_price":90,"amount":0.2,"reason":"第一档止损"}；末档用close_all=true省略amount。
    回执status是操作状态，order_status是订单状态；等待触发或submitted都不是成交。"""
    args = dict(side=side, order_type=order_type, reason=reason, amount=amount, close_all=close_all,
                price=price, trigger_price=trigger_price)
    return execute_action('close', args, config_id, symbol)


@tool('edit', args_schema=EditInput)
def edit_trade(order_id: str, reason: str, config_id: str, symbol: str,
               remaining_amount: float | None = None, price: float | None = None, trigger_price: float | None = None):
    """修改归属挂单，至少提供price/trigger_price/remaining_amount一项；省略保留。
    remaining_amount始终为目标未成交基础币量；all单不能改数量。普通限价不可改触发价，市价单不可改。
    例：{"order_id":"regular:123","remaining_amount":0.2,"price":101,"reason":"调整剩余入场"}。
    原生改单或条件撤建由能力决定；检查edit_method和新旧order_id。pending/unknown需核验，不重复提交。"""
    return execute_action('edit', dict(order_id=order_id, reason=reason, remaining_amount=remaining_amount,
                                       price=price, trigger_price=trigger_price), config_id, symbol)


@tool('cancel', args_schema=CancelInput)
def cancel_trade(order_id: str, reason: str, config_id: str, symbol: str):
    """撤销本任务入场/退出挂单，传回查询或回执的order_id和reason；不是平仓。
    普通/条件身份由系统路由，触发后的活跃子单经归属核验后撤其未成交部分。
    例：{"order_id":"algo:456","reason":"条件失效"}。只按真实回执报告结果，待核验不重发。"""
    return execute_action('cancel', dict(order_id=order_id, reason=reason), config_id, symbol)


CANONICAL_TOOLS = [open_trade, close_trade, edit_trade, cancel_trade]
