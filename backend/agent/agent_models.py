from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from langchain_core.messages import BaseMessage

class OpenOrderReal(BaseModel):
    """实盘限价开仓；可选成交后全仓 TP/SL，数量使用标的币数量。"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(gt=0, allow_inf_nan=False, description="入场的价格（限价单）")
    amount: float = Field(gt=0, allow_inf_nan=False, description="下单数量 (币种数量)")
    stop_loss: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="可选：同方向整仓SL；加仓省略则继承，填写则先更新整仓SL再加仓")
    take_profit: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="可选：同方向整仓TP；加仓省略则继承，填写则先更新整仓TP再加仓")
    reason: str = Field(description="开仓理由")

    @model_validator(mode="after")
    def validate_bracket(self):
        if self.stop_loss is not None:
            valid_sl = self.stop_loss < self.entry_price if self.action == "BUY_LIMIT" else self.stop_loss > self.entry_price
            if not valid_sl:
                raise ValueError("多单止损需低于入场；空单止损需高于入场")
        if self.take_profit is not None:
            valid_tp = self.take_profit > self.entry_price if self.action == "BUY_LIMIT" else self.take_profit < self.entry_price
            if not valid_tp:
                raise ValueError("多单止盈需高于入场；空单止盈需低于入场")
        return self

class OpenOrderSpotDCA(BaseModel):
    """现货定投开单参数；标的必须属于当前任务配置。"""
    symbol: Optional[str] = Field(None, min_length=1, description="配置内现货交易对，如 BTC/USDT；多标的任务每笔必须指定，仅单标的任务可省略")
    action: Literal["BUY_LIMIT"] = Field(description="BUY_LIMIT: 限价买入现货")
    entry_price: float = Field(gt=0, allow_inf_nan=False, description="入场的价格（限价单）")
    amount: float = Field(gt=0, allow_inf_nan=False, description="下单数量 (币种数量)")
    reason: str = Field(description="定投买入理由")

class OpenOrderStrategy(OpenOrderReal):
    """策略开仓，TP/SL 是否必填由任务退出模式校验。"""
    valid_duration_hours: int = Field(24, gt=0, le=168, description="挂单有效期(小时)，过期自动撤销")


class CloseOrder(BaseModel):
    """平仓的精确参数"""
    action: Literal["CLOSE"] = Field("CLOSE", description="固定为 CLOSE")
    pos_side: Literal["LONG", "SHORT"] = Field(description="你要平掉哪一个方向的仓位: LONG(平多), SHORT(平空)")
    entry_price: float = Field(0, ge=0, allow_inf_nan=False, description="旧参数兼容；新调用使用 exit_type 及 price/trigger_price")
    exit_type: Literal["market", "take_profit_limit", "stop_market"] | None = Field(None, description="显式指定即时市价、限价止盈或触发市价止损")
    price: float | None = Field(None, gt=0, allow_inf_nan=False, description="take_profit_limit 委托价格")
    trigger_price: float | None = Field(None, gt=0, allow_inf_nan=False, description="stop_market 触发价格")
    amount: float = Field(ge=0, allow_inf_nan=False, description="标的币数量；0表示该方向全部仓位")
    reason: str = Field(description="理由")

    @model_validator(mode="after")
    def validate_exit(self):
        if self.exit_type == "market" and (self.price is not None or self.trigger_price is not None or self.entry_price):
            raise ValueError("market 退出不接受价格")
        if self.exit_type == "take_profit_limit" and (self.price is None or self.trigger_price is not None):
            raise ValueError("take_profit_limit 必须提供 price，不能提供 trigger_price")
        if self.exit_type == "stop_market" and (self.trigger_price is None or self.price is not None):
            raise ValueError("stop_market 必须提供 trigger_price，不能提供 price")
        if self.exit_type is None and (self.price is not None or self.trigger_price is not None):
            raise ValueError("price/trigger_price 必须配合显式 exit_type")
        return self

class AdoptPositionRealSchema(BaseModel):
    """Explicitly adopt a verified existing perpetual position without trading."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pos_side: Literal["LONG", "SHORT"] = Field(description="已核对的现有持仓方向：LONG多仓，SHORT空仓")
    expected_amount: float = Field(gt=0, allow_inf_nan=False, strict=True,
                                   description="已核对的该方向全部标的币数量，不是合约张数；必须与交易所当前仓位一致")
    reason: str = Field(min_length=1, description="用户要求管理已有手动仓位的具体意图及接管原因")


class SessionTitle(BaseModel):
    """会话标题总结"""
    title: str = Field(description="总结后的会话标题，不超过6个字，不带标点")

class AgentState(BaseModel):
    symbol: str
    messages: List[BaseMessage]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    full_analysis: str = ""
    human_message: Optional[str] = None
    active_agent: Optional[str] = "MASTER"
    active_model_idx: Optional[int] = 0
    active_model_name: Optional[str] = ""
    spot_config_fingerprint: Optional[str] = None
