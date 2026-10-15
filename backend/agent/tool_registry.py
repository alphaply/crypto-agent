from __future__ import annotations

import json
import uuid
from typing import Any

from backend.agent.agent_tools import (
    DEFAULT_CANCEL_REASON,
    adopt_position_real,
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
from backend.agent.independent_tools import CANONICAL_TOOLS, READ_ONLY_TRADE_TOOLS
from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status
from backend.utils.logger import setup_logger


logger = setup_logger("ToolRegistry")


def canonical_tool_name(tool_name: str) -> str:
    """Accept case-only variants of canonical actions, without widening modes."""
    canonical = tool_name.lower()
    return canonical if canonical in {'open', 'close', 'edit', 'cancel'} else tool_name


def uses_scoped_admission(tool_name: str, config_id: str, arguments: dict | None = None) -> bool:
    from backend.config import config
    from backend.utils.exit_policy import effective_exit_mode
    tool_name = canonical_tool_name(tool_name)
    canonical = tool_name in {'open', 'close', 'edit', 'cancel'} or (
        tool_name == 'execute_trade_actions' and (arguments or {}).get('contract_version') == 2)
    cfg = config.get_config_by_id(config_id) or {}
    return canonical and cfg.get('mode', 'STRATEGY').upper() in {'REAL', 'STRATEGY'} and effective_exit_mode(cfg) == 'independent_exits'


_CANCEL_TOOL_NAMES = {"cancel_orders_real", "cancel_orders_strategy"}

_TOOLS_BY_MODE = {
    "REAL": [open_position_real, close_position_real, adopt_position_real, cancel_orders_real, update_position_protection_real, update_entry_order_real],
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
_TOOL_BY_NAME.update({tool.name: tool for tool in CANONICAL_TOOLS})


def get_trade_tools_for_mode(mode: str | None, config: dict | None = None, *, include_legacy: bool = False):
    if (config or {}).get('read_only'):
        return []
    trade_mode = str(mode or "STRATEGY").upper()
    if not include_legacy:
        from backend.agent.unified_trading import unified_schema_for_config
        cfg = {**(config or {}), 'mode': trade_mode}
        return [execute_trade_actions.model_copy(update={
            'args_schema': unified_schema_for_config(cfg),
            'description': '一次按顺序执行1–20项交易动作。contract_version=3；单笔用一项数组。'
                           '动作与参数按当前模式提供，数量是基础币；edit.remaining_amount为目标未成交量。'
                           '默认失败/待核验/未知停止；depends_on要求显式independent且此前项completed。'
                           '权威受理后及时返回，不等待限价成交，不回滚已执行项；系统后台核验原身份，不能换ID重发。'
                           '合法动作内部核验并自动纳管授权目标，无需先adopt。include=["account"]末尾可选一次账户摘要。',
        })]
    tools = list(_TOOLS_BY_MODE.get(trade_mode, _TOOLS_BY_MODE["STRATEGY"]))
    if config is None or trade_mode == 'SPOT_DCA':
        if include_legacy:
            tools.extend(tool for tool in [execute_trade_actions] if tool.name not in {item.name for item in tools})
        return tools
    from backend.utils.exit_policy import effective_exit_mode
    if effective_exit_mode(config) == 'independent_exits':
        preferred = [*CANONICAL_TOOLS, *([adopt_position_real] if trade_mode == 'REAL' else []), execute_trade_actions]
        if include_legacy:
            preferred.extend(tool for tool in tools if tool.name not in {item.name for item in preferred}
                             and not tool.name.startswith('update_position_protection_'))
        tools = preferred
    from backend.agent.tool_interfaces import specialize_trade_tool
    return [specialized for tool in tools
            if (specialized := specialize_trade_tool(tool, {**config, 'mode': trade_mode})) is not None]


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
                   cycle_id: str | None = None, expected_spot_fingerprint: str | None = None,
                   *, project_result: bool = True) -> str:
    tool_name = canonical_tool_name(tool_name)
    if tool_name in {'get_trade_operation', 'get_operation', 'get_paper_operation'}:
        return json.dumps({'status': 'failed', 'error_code': 'tool_removed',
                           'error': 'Operation query tools have been removed; the system updates original execution evidence automatically.'}, ensure_ascii=False)
    if tool_name == 'manage_trading_rules':
        return 'Error: Trading rule changes belong to the memory review agent; not allowed in trading or chat.'
    operation_id = operation_id or current_operation_id.get() or uuid.uuid4().hex
    from backend.config import config as runtime_config
    from backend.utils.spot_config_guard import spot_config_lock, spot_execution_fingerprint
    with spot_config_lock:
        config = runtime_config.get_config_by_id(config_id) or {}
        if expected_spot_fingerprint is not None and expected_spot_fingerprint != spot_execution_fingerprint(config):
            return json.dumps({'status': 'failed', 'error': '现货任务配置已变更，请重新分析后执行'}, ensure_ascii=False)
        if str(config.get('mode') or '').upper() == 'SPOT_DCA':
            return _run_trade_tool(tool_name, args, config_id, symbol, operation_id, cycle_id, project_result=project_result)
    return _run_trade_tool(tool_name, args, config_id, symbol, operation_id, cycle_id, project_result=project_result)


def _run_trade_tool(tool_name: str, args: Any, config_id: str, symbol: str, operation_id: str | None = None,
                    cycle_id: str | None = None, *, project_result: bool = True) -> str:
    from backend.config import config as runtime_config
    config = runtime_config.get_config_by_id(config_id)
    if config and tool_name not in {tool.name for tool in get_trade_tools_for_mode(config.get('mode'), config, include_legacy=True)}:
        return f"Error: Tool '{tool_name}' is not allowed for this trading mode."
    tool_obj = _TOOL_BY_NAME.get(tool_name)
    if not tool_obj:
        logger.warning("Tool call rejected: name=%s config_id=%s symbol=%s", tool_name, config_id, symbol)
        return f"Error: Tool '{tool_name}' not found."

    legacy_order_ids = _legacy_cancel_order_ids(tool_name, args)
    if legacy_order_ids is not None:
        if not 1 <= len(legacy_order_ids) <= 20:
            raise ValueError('Legacy cancellation arrays must contain 1–20 orders')
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
                results.append({'index': index, 'action': 'cancel', 'order_id': order_id,
                                'status': 'not_executed', 'operation_id': f'{operation_id}:{index}'})
                continue
            cancel_args = {"order_id": order_id, "reason": (args.get('reason') if isinstance(args, dict) else None) or DEFAULT_CANCEL_REASON}
            if isinstance(args, dict) and args.get('symbol'):
                cancel_args['symbol'] = args['symbol']
            result = run_trade_tool(tool_name, cancel_args, config_id, symbol,
                                    operation_id=f"{operation_id}:{index}", cycle_id=cycle_id, project_result=False)
            from backend.utils.trade_operations import _decode_receipt
            results.append({'index': index, 'action': 'cancel', 'operation_id': f'{operation_id}:{index}',
                            'order_id': order_id, 'status': tool_result_status(result), 'result': _decode_receipt(result)})
            stopped = tool_result_status(result) in {'failed', 'unknown', 'pending'}
        from backend.utils.trade_summary import compact_trade_receipt
        status = next((state for state in ('unknown', 'pending', 'failed', 'submitted')
                       if any(row['status'] == state for row in results)), 'completed')
        batch = {'operation_id': operation_id, 'status': status, 'results': results}
        return json.dumps(compact_trade_receipt(batch) if project_result else batch, ensure_ascii=False)

    call_args = _normalize_tool_args(tool_name, args)
    is_v3 = tool_name == 'execute_trade_actions' and call_args.get('contract_version') == 3
    if tool_name in {tool.name for tool in CANONICAL_TOOLS} or is_v3:
        if 'config_id' in call_args and call_args['config_id'] != config_id:
            raise ValueError('Conflicting trusted context: config_id')
        if 'symbol' in call_args and call_args['symbol'] != symbol:
            raise ValueError('Conflicting trusted context: symbol')
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
    if tool_name in {'open', 'close', 'edit', 'cancel', 'adopt_position_real', 'update_position_protection_real', 'update_position_protection_strategy', 'update_entry_order_real', 'update_entry_order_strategy', 'update_exit_order', 'execute_trade_actions'}:
        # Dispatch uses .func(), so these scalar arguments need explicit schema validation.
        supplied_version = 'contract_version' in call_args
        call_args = tool_obj.args_schema.model_validate(call_args).model_dump()
        if tool_name == 'execute_trade_actions' and not supplied_version:
            call_args.pop('contract_version', None)  # Preserve legacy operation hashes.
    call_args["config_id"] = config_id
    call_args["symbol"] = symbol

    from backend.mcp.guard import preflight_tool
    # Canonical adapters preflight inside the durable callback, so replay never
    # needs a formerly live edit target to still exist. Legacy hashes/flow stay intact.
    if tool_name not in {'open', 'close', 'edit', 'cancel', *READ_ONLY_TRADE_TOOLS} and call_args.get('contract_version') not in {2, 3}:
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
        if is_v3:
            # v3 owns its parent reservation. Legacy callbacks/hashes above stay unchanged.
            raw = tool_obj.func(**call_args)
        elif operation_id and tool_name not in READ_ONLY_TRADE_TOOLS:
            request = {'tool': tool_name, 'args': call_args}
            if tool_name in {'open', 'close', 'edit', 'cancel'} or call_args.get('contract_version') == 2:
                request['contract_version'] = 2
            raw = run_once(config_id, symbol, operation_id, request,
                           lambda: tool_obj.func(**call_args))
        else:
            raw = tool_obj.func(**call_args)
        if project_result:
            from backend.agent.unified_trading import compact_trade_receipt
            raw = compact_trade_receipt(raw)
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
