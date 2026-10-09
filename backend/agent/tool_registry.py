from __future__ import annotations

import json
from typing import Any

from backend.agent.agent_tools import (
    DEFAULT_CANCEL_REASON,
    cancel_orders_real,
    cancel_orders_spot,
    cancel_orders_strategy,
    close_position_real,
    close_position_strategy,
    open_position_real,
    open_position_spot_dca,
    open_position_strategy,
    update_position_protection_real,
    update_position_protection_strategy,
    update_entry_order_real,
    update_entry_order_strategy,
    update_exit_order,
)
from backend.agent.trade_batch import execute_trade_actions
from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status
from backend.utils.logger import setup_logger


logger = setup_logger("ToolRegistry")


_CANCEL_TOOL_NAMES = {"cancel_orders_real", "cancel_orders_strategy"}

_TOOLS_BY_MODE = {
    "REAL": [open_position_real, close_position_real, cancel_orders_real, update_position_protection_real, update_entry_order_real],
    "SPOT_DCA": [open_position_spot_dca, cancel_orders_spot],
    "STRATEGY": [open_position_strategy, cancel_orders_strategy, close_position_strategy, update_position_protection_strategy, update_entry_order_strategy],
}
for _mode, _mode_tools in _TOOLS_BY_MODE.items():
    if _mode in {"REAL", "STRATEGY"}:
        _mode_tools.extend([update_exit_order, execute_trade_actions])

_TOOL_BY_NAME = {
    tool.name: tool
    for tools in _TOOLS_BY_MODE.values()
    for tool in tools
}


def get_trade_tools_for_mode(mode: str | None):
    trade_mode = str(mode or "STRATEGY").upper()
    return list(_TOOLS_BY_MODE.get(trade_mode, _TOOLS_BY_MODE["STRATEGY"]))


def _normalize_tool_args(tool_name: str, args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        normalized = dict(args)
        if isinstance(normalized.get("orders"), str):
            try:
                normalized["orders"] = json.loads(normalized["orders"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"Tool '{tool_name}' orders must be a JSON array: {exc.msg}") from exc
        if tool_name in _CANCEL_TOOL_NAMES and "order_id" not in normalized and "cancel_order_id" in normalized:
            normalized["order_id"] = normalized.pop("cancel_order_id")
        if tool_name in _CANCEL_TOOL_NAMES and not normalized.get("reason"):
            normalized["reason"] = DEFAULT_CANCEL_REASON
        return normalized

    if tool_name in _CANCEL_TOOL_NAMES and isinstance(args, str):
        return {"order_id": args, "reason": DEFAULT_CANCEL_REASON}

    raise TypeError(f"Tool '{tool_name}' args must be an object.")


def _legacy_cancel_order_ids(tool_name: str, args: Any) -> list[str] | None:
    if tool_name not in _CANCEL_TOOL_NAMES:
        return None

    if isinstance(args, list):
        return [str(item) for item in args]

    if isinstance(args, dict) and "order_id" not in args and "order_ids" in args:
        value = args.get("order_ids")
        if isinstance(value, list):
            return [str(item) for item in value]
        if value:
            return [str(value)]

    return None


def _summarize_result(result: Any, limit: int = 300) -> str:
    text = str(result)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def run_trade_tool(tool_name: str, args: Any, config_id: str, symbol: str, operation_id: str | None = None,
                   cycle_id: str | None = None, expected_spot_fingerprint: str | None = None) -> str:
    if tool_name == 'manage_trading_rules':
        return 'Error: Trading rule changes belong to the memory review agent; not allowed in trading or chat.'
    from backend.config import config as runtime_config
    from backend.utils.spot_config_guard import spot_config_lock, spot_execution_fingerprint
    with spot_config_lock:
        config = runtime_config.get_config_by_id(config_id) or {}
        if expected_spot_fingerprint is not None and expected_spot_fingerprint != spot_execution_fingerprint(config):
            return json.dumps({'status': 'failed', 'error': '现货任务配置已变更，请重新分析后执行'}, ensure_ascii=False)
        if str(config.get('mode') or '').upper() == 'SPOT_DCA':
            return _run_trade_tool(tool_name, args, config_id, symbol, operation_id, cycle_id)
    return _run_trade_tool(tool_name, args, config_id, symbol, operation_id, cycle_id)


def _run_trade_tool(tool_name: str, args: Any, config_id: str, symbol: str, operation_id: str | None = None,
                    cycle_id: str | None = None) -> str:
    from backend.config import config as runtime_config
    config = runtime_config.get_config_by_id(config_id)
    if config and tool_name not in {tool.name for tool in get_trade_tools_for_mode(config.get('mode'))}:
        return f"Error: Tool '{tool_name}' is not allowed for this trading mode."
    tool_obj = _TOOL_BY_NAME.get(tool_name)
    if not tool_obj:
        logger.warning("Tool call rejected: name=%s config_id=%s symbol=%s", tool_name, config_id, symbol)
        return f"Error: Tool '{tool_name}' not found."

    legacy_order_ids = _legacy_cancel_order_ids(tool_name, args)
    if legacy_order_ids is not None:
        logger.info(
            "Expanding legacy cancel tool args: name=%s config_id=%s symbol=%s count=%s",
            tool_name,
            config_id,
            symbol,
            len(legacy_order_ids),
        )
        results = []
        stopped = False
        for index, order_id in enumerate(legacy_order_ids):
            if stopped:
                results.append(json.dumps({'order_id': order_id, 'status': 'not_executed'}))
                continue
            cancel_args = {"order_id": order_id, "reason": (args.get('reason') if isinstance(args, dict) else None) or DEFAULT_CANCEL_REASON}
            if isinstance(args, dict) and args.get('symbol'):
                cancel_args['symbol'] = args['symbol']
            result = run_trade_tool(tool_name, cancel_args, config_id, symbol,
                                    operation_id=f"{operation_id}:{index}" if operation_id else None, cycle_id=cycle_id)
            results.append(result)
            stopped = tool_result_status(result) in {'failed', 'unknown'}
        return "\n".join(results)

    call_args = _normalize_tool_args(tool_name, args)
    call_args.pop('config_id', None)
    requested_symbol = call_args.pop('symbol', None)
    is_spot = str((config or {}).get('mode', '')).upper() == 'SPOT_DCA'
    if is_spot:
        from backend.utils.spot_execution import resolve_spot_symbol
        from backend.utils.spot_portfolio import get_config_symbols
        try:
            if len(get_config_symbols(config)) > 1 and not requested_symbol:
                if tool_name in {'cancel_orders_spot', 'cancel_orders_real'}:
                    raise ValueError('多标的现货撤单必须明确指定 symbol')
                if tool_name == 'open_position_spot_dca' and any(
                    not (item.get('symbol') if isinstance(item, dict) else getattr(item, 'symbol', None))
                    for item in (call_args.get('orders') or [])
                ):
                    raise ValueError('多标的现货买入必须为每笔订单指定 symbol')
            symbol = resolve_spot_symbol(config, requested_symbol or symbol)
        except ValueError as exc:
            return json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False)
    if tool_name in {'update_position_protection_real', 'update_position_protection_strategy', 'update_entry_order_real', 'update_entry_order_strategy', 'update_exit_order', 'execute_trade_actions'}:
        # Dispatch uses .func(), so these scalar arguments need explicit schema validation.
        call_args = tool_obj.args_schema.model_validate(call_args).model_dump()
    call_args["config_id"] = config_id
    call_args["symbol"] = symbol

    from backend.mcp.guard import preflight_tool
    preflight_tool(tool_name, call_args, config_id, symbol)

    logger.info(
        "Executing tool call: name=%s config_id=%s symbol=%s arg_keys=%s",
        tool_name,
        config_id,
        symbol,
        sorted(call_args.keys()),
    )
    token = current_operation_id.set(operation_id)
    from backend.utils.spot_execution import current_spot_cycle_id
    cycle_token = current_spot_cycle_id.set(cycle_id) if is_spot else None
    try:
        if operation_id:
            raw = run_once(config_id, symbol, operation_id, {'tool': tool_name, 'args': call_args},
                           lambda: tool_obj.func(**call_args))
        else:
            raw = tool_obj.func(**call_args)
        result = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else str(raw)
    finally:
        if cycle_token is not None:
            current_spot_cycle_id.reset(cycle_token)
        current_operation_id.reset(token)
    logger.info(
        "Tool call completed: name=%s config_id=%s symbol=%s result=%s",
        tool_name,
        config_id,
        symbol,
        _summarize_result(result),
    )
    return result
