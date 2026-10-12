"""Version 2 independent trading inputs shared by Agent, batch and MCP."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_serializer, model_validator


PositiveNumber = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Reason = Annotated[str, Field(min_length=1)]
Side = Literal['LONG', 'SHORT']
OrderType = Literal['market', 'limit', 'take_profit_market', 'take_profit_limit', 'stop_market', 'stop_limit']


class TradingInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class OpenInput(TradingInput):
    side: Side = Field(description='持仓方向：LONG 开多，SHORT 开空；双向持仓要求 Hedge Mode')
    amount: PositiveNumber = Field(description='基础币数量，不是保证金或合约张数')
    price: PositiveNumber = Field(description='限价入场价格')
    reason: Reason


class CloseInput(TradingInput):
    side: Side = Field(description='要减少的持仓方向，LONG 平多、SHORT 平空')
    order_type: OrderType
    reason: Reason
    amount: PositiveNumber | None = Field(None, description='固定基础币数量；与 close_all=true 二选一')
    close_all: StrictBool | None = Field(None, description='仅允许 true；市价按提交前快照，条件市价在触发时全平')
    price: PositiveNumber | None = Field(None, description='普通/条件限价的委托价格')
    trigger_price: PositiveNumber | None = Field(None, description='条件单的触发价格；默认 CONTRACT_PRICE')

    @model_validator(mode='after')
    def validate_combination(self):
        if self.close_all is False or (self.amount is not None) == (self.close_all is True):
            raise ValueError('Choose exactly one: amount > 0 or close_all=true')
        if self.close_all and self.order_type not in {'market', 'stop_market', 'take_profit_market'}:
            raise ValueError('unsupported_combination: close_all requires market, stop_market or take_profit_market')
        needs_price = self.order_type in {'limit', 'stop_limit', 'take_profit_limit'}
        needs_trigger = self.order_type not in {'market', 'limit'}
        if (self.price is not None) != needs_price:
            raise ValueError(f'{self.order_type} ' + ('requires price' if needs_price else 'does not accept price'))
        if (self.trigger_price is not None) != needs_trigger:
            raise ValueError(f'{self.order_type} ' + ('requires trigger_price' if needs_trigger else 'does not accept trigger_price'))
        return self


class EditInput(TradingInput):
    order_id: str = Field(min_length=1, description='查询/回执中的当前有效订单身份，原样回传')
    reason: Reason
    remaining_amount: PositiveNumber | None = Field(None, description='目标未成交基础币数量；入场与退出含义一致；all 单不可设置')
    price: PositiveNumber | None = None
    trigger_price: PositiveNumber | None = None

    @model_validator(mode='after')
    def require_change(self):
        if all(value is None for value in (self.remaining_amount, self.price, self.trigger_price)):
            raise ValueError('Provide price, trigger_price or remaining_amount')
        return self


class CancelInput(TradingInput):
    order_id: str = Field(min_length=1, description='当前任务订单的规范身份，原样回传')
    reason: Reason


class BatchDependencies(TradingInput):
    depends_on: list[StrictInt] | None = None

    @model_serializer(mode='wrap')
    def omit_implicit(self, handler):
        result = handler(self)
        if 'depends_on' not in self.model_fields_set:
            result.pop('depends_on', None)
        return result


class OpenAction(OpenInput, BatchDependencies):
    action: Literal['open']


class CloseAction(CloseInput, BatchDependencies):
    action: Literal['close']


class EditAction(EditInput, BatchDependencies):
    action: Literal['edit']


class CancelAction(CancelInput, BatchDependencies):
    action: Literal['cancel']


TradeAction = Annotated[OpenAction | CloseAction | EditAction | CancelAction, Field(discriminator='action')]


class CanonicalTradeActionsSchema(TradingInput):
    contract_version: Literal[2] = Field(description='新平铺动作必须显式填写 2；不接受旧 order 嵌套格式')
    actions: list[TradeAction] = Field(min_length=1, max_length=20)
    execution_policy: Literal['stop_on_error', 'independent'] = 'stop_on_error'

    @model_validator(mode='after')
    def validate_dependencies(self):
        for index, action in enumerate(self.actions):
            if 'depends_on' not in action.model_fields_set:
                continue
            if self.execution_policy != 'independent' or action.depends_on is None:
                raise ValueError('depends_on requires independent policy and an index list')
            dependencies = action.depends_on
            if len(set(dependencies)) != len(dependencies) or any(i < 0 or i >= index for i in dependencies):
                raise ValueError('depends_on must contain unique earlier action indices')
        return self

    @model_serializer(mode='wrap')
    def omit_implicit(self, handler):
        result = handler(self)
        if 'execution_policy' not in self.model_fields_set:
            result.pop('execution_policy', None)
        return result


CANONICAL_MODELS = {'open': OpenInput, 'close': CloseInput, 'edit': EditInput, 'cancel': CancelInput}


def contract_version(tool_name: str, arguments: dict) -> int:
    return 2 if tool_name in CANONICAL_MODELS else int(arguments.get('contract_version', 1))
