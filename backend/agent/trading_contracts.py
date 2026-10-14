"""Version 2 independent trading inputs shared by Agent, batch and MCP."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_serializer, model_validator


PositiveNumber = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
Reason = Annotated[str, Field(min_length=1)]
Side = Literal['LONG', 'SHORT']
OrderType = Literal['market', 'limit', 'take_profit_market', 'take_profit_limit', 'stop_market', 'stop_limit']
EXECUTION_POLICY_DESCRIPTION = (
    '省略或 stop_on_error：失败/待核验/未知停止后续，不允许任何动作填写 depends_on。'
    '使用 depends_on 必须显式选择 independent：未满足依赖项不执行，其他无依赖项继续逐项准入。'
    '任务的 independent_exits 模式不会自动选择此策略。'
)


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
    depends_on: list[StrictInt] = Field(default_factory=list, description=(
        '可省略；提供时要求批次顶层显式 execution_policy="independent"。必须为非 null 整数数组；'
        '索引从0开始，只能引用不重复且小于本动作索引的前项，前项必须 completed，submitted 不满足依赖。'
        '无依赖时省略；不接受 null、布尔值或字符串。'))

    @field_validator('depends_on', mode='before')
    @classmethod
    def require_index_list(cls, value):
        if value is None:
            raise ValueError('depends_on must be an index list, not null; omit the field when no dependency is intended')
        return value

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
    execution_policy: Literal['stop_on_error', 'independent'] = Field('stop_on_error', description=EXECUTION_POLICY_DESCRIPTION)

    @model_validator(mode='after')
    def validate_dependencies(self):
        for index, action in enumerate(self.actions):
            if 'depends_on' not in action.model_fields_set:
                continue
            if self.execution_policy != 'independent':
                raise ValueError(f'actions[{index}].depends_on requires explicit execution_policy="independent"; '
                                 'omitted policy defaults to stop_on_error. Omit depends_on only if no dependency is intended')
            dependencies = action.depends_on
            if len(set(dependencies)) != len(dependencies) or any(i < 0 or i >= index for i in dependencies):
                raise ValueError(f'actions[{index}].depends_on must contain unique earlier action indices '
                                 f'(zero-based integers >= 0 and < {index})')
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


# Version 3 is deliberately separate: adding defaults to the v2 models changes
# persisted request hashes and makes old replay unsafe.
class UnifiedActionBase(BatchDependencies):
    symbol: str | None = Field(None, min_length=1, max_length=100)
    market_type: Literal['spot', 'swap'] | None = None
    exchange: Literal['binance', 'okx', 'bitget', 'bybit'] | None = None
    reason: Reason

    @field_validator('symbol', mode='after')
    @classmethod
    def literal_symbol(cls, value):
        if value and any(marker in value for marker in ('${', '{{', '$ref', '\n')):
            raise ValueError('symbol must be a literal authorized market, not a result reference')
        return value


class UnifiedOpenAction(UnifiedActionBase):
    action: Literal['open']
    side: Side
    order_type: Literal['market', 'limit'] = 'limit'
    amount: PositiveNumber | None = None
    quote_amount: PositiveNumber | None = None
    margin_amount: PositiveNumber | None = None
    price: PositiveNumber | None = None
    leverage: PositiveNumber | None = None
    stop_loss: PositiveNumber | None = None
    take_profit: PositiveNumber | None = None
    valid_duration_hours: Annotated[int, Field(strict=True, gt=0, le=168)] | None = None

    @model_validator(mode='after')
    def validate_entry(self):
        if sum(value is not None for value in (self.amount, self.quote_amount, self.margin_amount)) != 1:
            raise ValueError('open requires exactly one sizing input: amount, quote_amount or margin_amount')
        if (self.price is not None) != (self.order_type == 'limit'):
            raise ValueError('limit requires price; market does not accept price')
        for field, value in (('stop_loss', self.stop_loss), ('take_profit', self.take_profit)):
            if value is not None and self.price is not None:
                below = (field == 'stop_loss') == (self.side == 'LONG')
                if (below and value >= self.price) or (not below and value <= self.price):
                    raise ValueError(f'{field} is on the wrong side of the entry price')
        return self


class UnifiedCloseAction(UnifiedActionBase):
    action: Literal['close']
    side: Side
    order_type: Literal['market', 'limit', 'take_profit_market', 'take_profit_limit',
                        'stop_market', 'stop_limit', 'stop_loss', 'take_profit', 'oco']
    amount: PositiveNumber | None = None
    close_all: StrictBool | None = None
    price: PositiveNumber | None = None
    trigger_price: PositiveNumber | None = None
    stop_loss: PositiveNumber | None = None
    take_profit: PositiveNumber | None = None
    stop_limit_price: PositiveNumber | None = None
    take_profit_limit_price: PositiveNumber | None = None

    @model_validator(mode='after')
    def validate_exit(self):
        if self.close_all is False or (self.amount is not None) == (self.close_all is True):
            raise ValueError('close requires exactly one: amount > 0 or close_all=true')
        kind = self.order_type
        allowed = ({'price'} if kind == 'limit' else
                   {'trigger_price'} if kind in {'stop_market', 'take_profit_market'} else
                   {'price', 'trigger_price'} if kind in {'stop_limit', 'take_profit_limit'} else
                   {'trigger_price', 'stop_loss', 'stop_limit_price'} if kind == 'stop_loss' else
                   {'trigger_price', 'take_profit', 'take_profit_limit_price'} if kind == 'take_profit' else
                   {'stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price'} if kind == 'oco' else set())
        for field in ('price', 'trigger_price', 'stop_loss', 'take_profit', 'stop_limit_price', 'take_profit_limit_price'):
            if getattr(self, field) is not None and field not in allowed:
                raise ValueError(f'{kind} does not accept {field}')
        required = {'limit': ('price',), 'stop_market': ('trigger_price',), 'take_profit_market': ('trigger_price',),
                    'stop_limit': ('price', 'trigger_price'), 'take_profit_limit': ('price', 'trigger_price'),
                    'oco': ('stop_loss', 'take_profit')}.get(kind, ())
        for field in required:
            if getattr(self, field) is None:
                raise ValueError(f'{kind} requires {field}')
        if kind in {'stop_loss', 'take_profit'}:
            if (self.trigger_price is None) == (getattr(self, kind) is None):
                raise ValueError(f'{kind} requires exactly one trigger_price or {kind}')
        if self.stop_loss and self.take_profit and self.stop_loss >= self.take_profit:
            raise ValueError('stop_loss must be below take_profit for spot OCO')
        return self


class UnifiedEditAction(UnifiedActionBase):
    action: Literal['edit']
    order_id: str = Field(min_length=1, max_length=200)
    remaining_amount: PositiveNumber | None = Field(None, description='目标未成交基础币数量；依据最新实际成交计算总量，all 单不可设置')
    price: PositiveNumber | None = None
    trigger_price: PositiveNumber | None = None
    stop_loss: PositiveNumber | None = None
    take_profit: PositiveNumber | None = None

    @model_validator(mode='after')
    def require_change(self):
        if all(value is None for value in (self.remaining_amount, self.price, self.trigger_price, self.stop_loss, self.take_profit)):
            raise ValueError('edit requires an explicit supported price, quantity or protection change')
        return self


class UnifiedCancelAction(UnifiedActionBase):
    action: Literal['cancel']
    order_id: str = Field(min_length=1, max_length=200)


class UnifiedProtectAction(UnifiedActionBase):
    action: Literal['protect']
    side: Side
    order_id: str | None = Field(None, min_length=1, max_length=200)
    amount: PositiveNumber | None = None
    close_all: StrictBool | None = None
    stop_loss: PositiveNumber | None = None
    take_profit: PositiveNumber | None = None
    stop_limit_price: PositiveNumber | None = None
    take_profit_limit_price: PositiveNumber | None = None

    @model_validator(mode='after')
    def validate_protection(self):
        if self.stop_loss is None and self.take_profit is None:
            raise ValueError('protect requires stop_loss and/or take_profit')
        if self.close_all is False or (self.amount is not None and self.close_all is True):
            raise ValueError('protect amount and close_all=true are mutually exclusive')
        return self


UnifiedTradeAction = Annotated[
    UnifiedOpenAction | UnifiedCloseAction | UnifiedEditAction | UnifiedCancelAction | UnifiedProtectAction,
    Field(discriminator='action'),
]


class UnifiedTradeActionsSchema(CanonicalTradeActionsSchema):
    """Fresh contract; v1/v2 serializers above retain their exact old meaning."""
    contract_version: Literal[3] = Field(description='显式整数3；使用当前可信模式的平铺动作，不覆盖账户或引擎')
    actions: list[UnifiedTradeAction] = Field(min_length=1, max_length=20)
    include: list[Literal['account']] = Field(default_factory=list, max_length=1)

    @model_validator(mode='before')
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and (type(value.get('contract_version')) is not int or value.get('contract_version') != 3):
            raise ValueError('contract_version must explicitly be integer 3')
        return value

    @model_serializer(mode='wrap')
    def omit_implicit(self, handler):
        result = handler(self)
        for field in ('execution_policy', 'include'):
            if field not in self.model_fields_set:
                result.pop(field, None)
        return result
