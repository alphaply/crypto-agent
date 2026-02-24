from typing import Any, Dict, List, Literal, Optional
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

class OrderParams(BaseModel):
    reason: str = Field(description="简短的执行理由")
    action: Literal["BUY_LIMIT", "SELL_LIMIT", "CLOSE", "CANCEL", "NO_ACTION"] = Field(
        description="BUY_LIMIT现价开多、SELL_LIMIT现价开空、CLOSE进行平多平空(只有仓位时才有效)、NO_ACTION不做任何事情、CANCEL取消挂单"
    )
    pos_side: Optional[Literal["LONG", "SHORT"]] = Field(description="平仓方向: CLOSE时必填", default=None)
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="挂单价格/平仓价格", default=0.0)
    amount: float = Field(description="下单数量", default=0.0)
    take_profit: float = Field(description="计划止盈位", default=0.0)
    stop_loss: float = Field(description="计划止损位", default=0.0)
    valid_duration_hours: int = Field(description="挂单有效期(小时)", default=24)

class AgentState(BaseModel):
    config_id: str
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    
    # 简化后的字段
    full_analysis: str = "" # LLM 输出的完整分析文本
