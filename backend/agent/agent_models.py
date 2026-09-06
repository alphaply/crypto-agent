from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, model_validator
from langchain_core.messages import (
    BaseMessageChunk,
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
    trim_messages,
    BaseMessage
)

class OpenOrderReal(BaseModel):
    """限价开仓与成交后全仓 TP/SL 计划；数量使用标的币数量。"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(gt=0, allow_inf_nan=False, description="入场的价格（限价单）")
    amount: float = Field(gt=0, allow_inf_nan=False, description="下单数量 (币种数量)")
    stop_loss: float = Field(gt=0, allow_inf_nan=False, description="成交后止损触发价；保护同方向整个仓位")
    take_profit: float = Field(gt=0, allow_inf_nan=False, description="成交后止盈触发价；保护同方向整个仓位")
    reason: str = Field(description="开仓理由")

    @model_validator(mode="after")
    def validate_bracket(self):
        valid = (self.stop_loss < self.entry_price < self.take_profit if self.action == "BUY_LIMIT"
                 else self.take_profit < self.entry_price < self.stop_loss)
        if not valid:
            raise ValueError("多单需要 SL < 入场 < TP；空单需要 TP < 入场 < SL")
        return self

class OpenOrderSpotDCA(BaseModel):
    """现货定投开单参数：仅包含买入限价单核心参数"""
    action: Literal["BUY_LIMIT"] = Field(description="BUY_LIMIT: 限价买入现货")
    entry_price: float = Field(description="入场的价格（限价单）")
    amount: float = Field(description="下单数量 (币种数量)")
    reason: str = Field(description="定投买入理由")

class OpenOrderStrategy(OpenOrderReal):
    """策略模式开仓参数：包含止盈止损和有效期"""
    valid_duration_hours: int = Field(24, gt=0, le=168, description="挂单有效期(小时)，过期自动撤销")

class CloseOrder(BaseModel):
    """平仓的精确参数"""
    action: Literal["CLOSE"] = Field("CLOSE", description="固定为 CLOSE")
    pos_side: Literal["LONG", "SHORT"] = Field(description="你要平掉哪一个方向的仓位: LONG(平多), SHORT(平空)")
    entry_price: float = Field(ge=0, allow_inf_nan=False, description="0表示市价退出；正值为平仓委托价，非已成交价格")
    amount: float = Field(ge=0, allow_inf_nan=False, description="标的币数量；0表示该方向全部仓位")
    reason: str = Field(description="理由")

class SessionTitle(BaseModel):
    """会话标题总结"""
    title: str = Field(description="总结后的会话标题，不超过6个字，不带标点")

class AgentState(BaseModel):
    symbol: str
    messages: List[BaseMessage]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    full_analysis: str = ""
    human_message: Optional[str] = None
    active_agent: Optional[str] = "MASTER"
