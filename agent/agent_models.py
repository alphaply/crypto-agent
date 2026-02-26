from typing import Any, Dict, List, Literal, Optional
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

class OpenOrderReal(BaseModel):
    """实盘开仓参数：仅包含限价单核心参数，不包含止盈止损（实盘暂不支持自动带止盈止损）"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(description="入场的价格（限价单）")
    amount: float = Field(description="下单数量 (标的币种数量)")
    reason: str = Field(description="开仓理由")

class OpenOrderStrategy(BaseModel):
    """策略模式开仓参数：包含止盈止损和有效期"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(description="入场的价格")
    amount: float = Field(description="下单数量 (标的币种数量)")
    take_profit: Optional[float] = Field(None, description="计划止盈价格")
    stop_loss: Optional[float] = Field(None, description="计划止损价格")
    valid_duration_hours: int = Field(24, description="挂单有效期(小时)，过期自动撤销")
    reason: str = Field(description="开仓理由")

class CloseOrder(BaseModel):
    """平仓的精确参数"""
    action: Literal["CLOSE"] = Field("CLOSE", description="固定为 CLOSE")
    pos_side: Literal["LONG", "SHORT"] = Field(description="你要平掉哪一个方向的仓位: LONG(平多), SHORT(平空)")
    entry_price: float = Field(description="触发平仓的价格，自动挂上限价订单")
    amount: float = Field(description="数量 (标的币种数量)")
    reason: str = Field(description="理由")

class AgentState(BaseModel):
    config_id: str
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    full_analysis: str = ""
    human_message: Optional[str] = None
