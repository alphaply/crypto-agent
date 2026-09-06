from __future__ import annotations

import json
from typing import Any

from backend.agent.agent_tools import (
    DEFAULT_CANCEL_REASON,
    cancel_orders_real,
    cancel_orders_strategy,
    close_position_real,
    close_position_strategy,
    open_position_real,
    open_position_spot_dca,
    open_position_strategy,
    update_position_protection_real,
    update_position_protection_strategy,
)
from backend.utils.logger import setup_logger


logger = setup_logger("ToolRegistry")


_CANCEL_TOOL_NAMES = {"cancel_orders_real", "cancel_orders_strategy"}

_TOOLS_BY_MODE = {
    "REAL": [open_position_real, close_position_real, cancel_orders_real, update_position_protection_real],
    "SPOT_DCA": [open_position_spot_dca, cancel_orders_real],
    "STRATEGY": [open_position_strategy, cancel_orders_strategy, close_position_strategy, update_position_protection_strategy],
}

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


def run_trade_tool(tool_name: str, args: Any, config_id: str, symbol: str) -> str:
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
        return "\n".join(
            run_trade_tool(tool_name, {"order_id": order_id, "reason": DEFAULT_CANCEL_REASON}, config_id, symbol)
            for order_id in legacy_order_ids
        )

    call_args = _normalize_tool_args(tool_name, args)
    if tool_name in {'update_position_protection_real', 'update_position_protection_strategy'}:
        # Dispatch uses .func(), so these scalar arguments need explicit schema validation.
        call_args = tool_obj.args_schema.model_validate(call_args).model_dump()
    call_args["config_id"] = config_id
    call_args["symbol"] = symbol

    logger.info(
        "Executing tool call: name=%s config_id=%s symbol=%s arg_keys=%s",
        tool_name,
        config_id,
        symbol,
        sorted(call_args.keys()),
    )
    result = str(tool_obj.func(**call_args))
    logger.info(
        "Tool call completed: name=%s config_id=%s symbol=%s result=%s",
        tool_name,
        config_id,
        symbol,
        _summarize_result(result),
    )
    return result
