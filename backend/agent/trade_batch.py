"""One ordered, validated request for multiple trading operations."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from backend.agent.agent_models import CloseOrder, OpenOrderStrategy
from backend.utils.trade_operations import current_operation_id, run_once, tool_result_status


class ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenAction(ActionBase):
    action: Literal["open"]
    order: OpenOrderStrategy


class CloseAction(ActionBase):
    action: Literal["close"]
    order: CloseOrder


class AmendAction(ActionBase):
    action: Literal["amend_entry", "amend_exit"]
    order_id: str = Field(min_length=1)
    pos_side: Literal["LONG", "SHORT"] | None = None
    entry_price: float | None = Field(None, gt=0, allow_inf_nan=False)
    amount: float | None = Field(None, gt=0, allow_inf_nan=False)
    price: float | None = Field(None, gt=0, allow_inf_nan=False)
    trigger_price: float | None = Field(None, gt=0, allow_inf_nan=False)
    stop_loss: float | None = Field(None, gt=0, allow_inf_nan=False)
    take_profit: float | None = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_fields(self):
        if self.action == "amend_entry":
            if self.price is not None or self.trigger_price is not None:
                raise ValueError("入场改单使用 entry_price")
            values = (self.entry_price, self.amount, self.stop_loss, self.take_profit)
        else:
            if any(v is not None for v in (self.entry_price, self.stop_loss, self.take_profit)):
                raise ValueError("退出改单使用 price/trigger_price")
            if self.price is not None and self.trigger_price is not None:
                raise ValueError("退出改单不能同时提供限价与触发价格")
            values = (self.price, self.trigger_price, self.amount)
        if all(v is None for v in values):
            raise ValueError("至少提供一个修改字段")
        return self


class CancelAction(ActionBase):
    action: Literal["cancel"]
    order_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ProtectionAction(ActionBase):
    action: Literal["update_protection"]
    pos_side: Literal["LONG", "SHORT"] | None = None
    order_id: str | None = None
    stop_loss: float | None = Field(None, gt=0, allow_inf_nan=False)
    take_profit: float | None = Field(None, gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_change(self):
        if self.stop_loss is None and self.take_profit is None:
            raise ValueError("保护调整至少提供 TP 或 SL")
        return self


TradeAction = Annotated[OpenAction | CloseAction | AmendAction | CancelAction | ProtectionAction, Field(discriminator="action")]


class LegacyTradeActionsSchema(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actions: list[TradeAction] = Field(min_length=1, max_length=20)


class TradeActionsSchema(BaseModel):
    """Execution envelope; recommended v2 and legacy have separate strict parsers."""
    model_config = ConfigDict(extra='forbid')
    contract_version: Literal[1, 2] = 1
    actions: list[dict] = Field(min_length=1, max_length=20)
    execution_policy: Literal['stop_on_error', 'independent'] = 'stop_on_error'

    @model_serializer(mode='wrap')
    def omit_implicit(self, handler):
        result = handler(self)
        if 'execution_policy' not in self.model_fields_set:
            result.pop('execution_policy', None)
        return result

    @model_validator(mode='before')
    @classmethod
    def select_contract(cls, value):
        from backend.agent.trading_contracts import CanonicalTradeActionsSchema
        if not isinstance(value, dict):
            return value
        version = value.get('contract_version', 1)
        if type(version) is not int or version not in {1, 2}:
            raise ValueError('contract_version must be 1 or 2')
        if version == 2:
            return CanonicalTradeActionsSchema.model_validate(value).model_dump()
        legacy = LegacyTradeActionsSchema.model_validate({key: item for key, item in value.items() if key != 'contract_version'})
        return {**legacy.model_dump(), **({'contract_version': 1} if 'contract_version' in value else {})}


def _validate_batch_direction(actions, config_id, symbol, mode):
    if mode != 'REAL':
        return
    sides = {action.side if hasattr(action, 'side') else ('LONG' if action.order.action == 'BUY_LIMIT' else 'SHORT')
             for action in actions if action.action == 'open'}
    if len(sides) > 1:
        from backend.agent.agent_tools import MarketTool
        if not MarketTool(config_id=config_id).exchange.fetch_position_mode(symbol).get('hedged'):
            raise ValueError('One-way mode does not support a batch of both LONG and SHORT entries')


def _execute_v2(actions, config_id, symbol, mode, execution_policy=None):
    from backend.agent.trading_contracts import CanonicalTradeActionsSchema
    from backend.agent.independent_tools import execute_action
    from backend.utils.exit_policy import effective_exit_mode
    from backend.config import config
    if effective_exit_mode(config.get_config_by_id(config_id)) != 'independent_exits':
        raise ValueError('Version 2 actions require independent_exits')
    envelope = {'contract_version': 2, 'actions': actions}
    if execution_policy is not None:
        envelope['execution_policy'] = execution_policy
    parsed = CanonicalTradeActionsSchema.model_validate(envelope)
    independent = parsed.execution_policy == 'independent'
    from backend.mcp.guard import preflight_tool
    try:
        preflight_tool('execute_trade_actions', parsed.model_dump(), config_id, symbol)
        _validate_batch_direction(parsed.actions, config_id, symbol, mode)
    except (ValueError, PermissionError) as exc:
        return json.dumps({'contract_version': 2, 'status': 'failed', 'error': str(exc),
                           'results': [{'index': index, 'action': item.action, 'status': 'not_executed'}
                                       for index, item in enumerate(parsed.actions)]}, ensure_ascii=False)
    import uuid
    batch_id = current_operation_id.get() or uuid.uuid4().hex
    results, blocked_by = [], None
    for index, action in enumerate(parsed.actions):
        if blocked_by is not None:
            results.append({'index': index, 'action': action.action, 'status': 'not_executed', 'blocked_by_index': blocked_by})
            continue
        dependencies = action.depends_on or []
        unmet = [i for i in dependencies if results[i]['status'] != 'completed']
        if unmet:
            results.append({'index': index, 'action': action.action, 'status': 'not_executed',
                            'reason_code': 'dependency_not_completed', 'blocked_by_indices': unmet})
            continue
        args = action.model_dump(exclude={'action', 'depends_on'}, exclude_none=True)
        raw = run_once(config_id, symbol, f'{batch_id}:{index}',
                       {'contract_version': 2, 'action': action.action, 'arguments': args},
                       lambda a=action.action, values=args: execute_action(a, values, config_id, symbol))
        status = tool_result_status(raw)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if independent and result.get('error_code') == 'trade_recovery_required':
            status = 'not_executed'
        results.append({'index': index, 'action': action.action, 'status': status, 'result': result})
        if not independent and status in {'failed', 'unknown', 'pending'}:
            blocked_by = index
    status = next((row['status'] for row in results if row['status'] in {'failed', 'unknown', 'pending'}),
                  'submitted' if any(row['status'] == 'submitted' for row in results) else 'completed')
    if independent:
        status = next((s for s in ('unknown', 'pending', 'failed') if any(r['status'] == s for r in results)),
                      'failed' if any(r['status'] == 'not_executed' for r in results) else status)
    return json.dumps({'contract_version': 2, 'status': status, 'operation_id': batch_id,
                       'execution_policy': parsed.execution_policy, 'results': results,
                       'summary': {s: sum(r['status'] == s for r in results) for s in
                                   ('completed', 'submitted', 'failed', 'unknown', 'pending', 'not_executed', 'skipped')}}, ensure_ascii=False)


def _dispatch(action, config_id, symbol, mode):
    from backend.agent import agent_tools as t
    args = action.model_dump(exclude_none=True)
    kind = args.pop("action")
    suffix = "real" if mode == "REAL" else "strategy"
    if kind in {"open", "close"}:
        order = args.pop("order")
        if kind == "open" and mode == "REAL":
            order.pop("valid_duration_hours", None)
        return getattr(t, f"{kind}_position_{suffix}").func(orders=[order], config_id=config_id, symbol=symbol)
    if kind == "cancel":
        return getattr(t, f"cancel_orders_{suffix}").func(config_id=config_id, symbol=symbol, **args)
    if kind == "amend_exit":
        args.pop("pos_side", None)
        return t.update_exit_order.func(config_id=config_id, symbol=symbol, **args)
    if kind == "amend_entry":
        return getattr(t, f"update_entry_order_{suffix}").func(config_id=config_id, symbol=symbol, **args)
    if mode == "REAL":
        args.pop("order_id", None)
    else:
        args.pop("pos_side", None)
    return getattr(t, f"update_position_protection_{suffix}").func(config_id=config_id, symbol=symbol, **args)


@tool(args_schema=TradeActionsSchema)
def execute_trade_actions(actions: list, config_id: str, symbol: str, contract_version: int = 1,
                          execution_policy: str | None = None):
    """一次顺序执行开/加仓(open)、部分退出(close)、撤单(cancel)、入场/退出改单(amend_entry/amend_exit)、保护调整(update_protection)。
    退出仅针对已成交仓位；数量为标的币数量。先校验整批，默认失败停止；v2可选execution_policy=independent，逐项核验实际依赖，depends_on仅接受已完成前项的索引。
    普通调用自动取证，无需提前查询；已提交不等于已成交，多个交易所写入不是原子事务。
    返回逐项真实结果和状态；未知操作等待核验，不得换工具或换ID盲目重试。"""
    from backend.config import config
    from backend.utils.exit_policy import effective_exit_mode, validate_entry_protection
    cfg = config.get_config_by_id(config_id)
    if not cfg or cfg.get("mode", "STRATEGY").upper() not in {"REAL", "STRATEGY"}:
        raise ValueError("批量交易仅适用于已有 REAL/STRATEGY 任务")
    values = {'actions': actions, 'contract_version': contract_version}
    if execution_policy is not None:
        values['execution_policy'] = execution_policy
    envelope = TradeActionsSchema.model_validate(values)
    if envelope.contract_version == 2:
        return _execute_v2(envelope.actions, config_id, symbol, cfg.get('mode', 'STRATEGY').upper(), execution_policy)
    parsed = LegacyTradeActionsSchema.model_validate({"actions": envelope.actions})
    from backend.mcp.guard import preflight_tool
    preflight_tool('execute_trade_actions', parsed.model_dump(), config_id, symbol)
    mode = cfg.get("mode", "STRATEGY").upper()
    _validate_batch_direction(parsed.actions, config_id, symbol, mode)
    exit_mode = effective_exit_mode(cfg)
    for action in parsed.actions:
        if action.action == "open":
            validate_entry_protection(cfg, action.order)
        if action.action == "update_protection":
            if effective_exit_mode(cfg) == "independent_exits":
                raise ValueError("独立退出模式使用 close/amend_exit，不使用整仓保护")
            if not (action.pos_side if mode == "REAL" else action.order_id):
                raise ValueError("保护调整缺少方向或模拟订单 ID")
        if action.action == "amend_entry" and mode == "STRATEGY" and not action.pos_side:
            raise ValueError("模拟入场改单必须提供 pos_side")
        if action.action == "amend_exit" and effective_exit_mode(cfg) != "independent_exits":
            raise ValueError("退出改单要求独立退出模式")
        if action.action == "amend_entry" and mode == "REAL" and (action.stop_loss is not None or action.take_profit is not None):
            raise ValueError("实盘入场和保护调整请使用独立动作")
        if action.action == "amend_entry" and exit_mode == "independent_exits" and (action.stop_loss is not None or action.take_profit is not None):
            raise ValueError("独立退出模式不在入场单附带 TP/SL")
        if action.action == 'close' and mode == 'STRATEGY' and exit_mode != 'independent_exits' and action.order.exit_type in {'take_profit_limit', 'stop_market'}:
            raise ValueError('模拟限价/止损退出单要求独立退出模式')
    import uuid
    batch_id = current_operation_id.get() or uuid.uuid4().hex
    results = []
    blocked_by_index = None
    for i, action in enumerate(parsed.actions):
        if blocked_by_index is not None:
            results.append({"index": i, "action": action.action, "status": "not_executed", "blocked_by_index": blocked_by_index})
            continue
        result = run_once(config_id, symbol, f"{batch_id}:{i}", action.model_dump(), lambda a=action: _dispatch(a, config_id, symbol, mode))
        status = tool_result_status(result)
        results.append({"index": i, "action": action.action, "status": status, "result": result})
        if status in {"failed", "unknown", "pending"}:
            blocked_by_index = i
    status = next((r["status"] for r in results if r["status"] in {"failed", "unknown", "pending"}),
                  'submitted' if any(r['status'] == 'submitted' for r in results) else 'completed')
    return json.dumps({"status": status, "operation_id": batch_id, "results": results}, ensure_ascii=False)
