from typing import Any, Dict, List, Literal, Optional
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

class OpenOrder(BaseModel):
    """开多或开空的精确参数"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(description="期待入场的价格 (必须根据当前市场价设定合理的价格)")
    amount: float = Field(description="下单数量 (标的币种数量)")
    take_profit: Optional[float] = Field(None, description="计划止盈价格 (可选)")
    stop_loss: Optional[float] = Field(None, description="计划止损价格 (可选)")
    reason: str = Field(description="为什么要在这个位置开仓的简短理由")

class CloseOrder(BaseModel):
    """平仓的精确参数"""
    action: Literal["CLOSE"] = Field("CLOSE", description="固定为 CLOSE")
    pos_side: Literal["LONG", "SHORT"] = Field(description="你要平掉哪一个方向的仓位: LONG(平多), SHORT(平空)")
    entry_price: float = Field(description="平仓的价格")
    amount: float = Field(description="平仓数量 (标的币种数量)")
    reason: str = Field(description="平仓理由")

class AgentState(BaseModel):
    config_id: str
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    full_analysis: str = ""
