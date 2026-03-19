from typing import Any, Dict, List, Literal, Optional
from typing import List, Literal, Optional, Any, Dict
from pydantic import BaseModel, Field
from langchain_core.messages import (
    BaseMessageChunk,
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
    trim_messages,
    BaseMessage
)

class ScreenerResult(BaseModel):
    """Screener 结构化输出"""
    confidence: int = Field(ge=0, le=100, description="市场出现交易机会的置信度 (0-100)")
    should_escalate: bool = Field(default=False, description="是否请求强大的大模型进行深度分析（当发现明确机会或账户面临风险时设为 True）")
    market_status: str = Field(description="当前市场状态的简短描述，用于前端展示")
    analysis: str = Field(description="对当前行情的详细分析，包括趋势、支撑压力位等")
    prediction: str = Field(description="对未来走势的预测")
    reason: str = Field(description="做出是否接入大模型判断的理由")

class OpenOrderReal(BaseModel):
    """开单参数：仅包含限价单核心参数，不包含止盈止损（暂不支持自动带止盈止损）"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(description="入场的价格（限价单）")
    amount: float = Field(description="下单数量 (币种数量)")
    reason: str = Field(description="开仓理由")

class OpenOrderSpotDCA(BaseModel):
    """现货定投开单参数：仅包含买入限价单核心参数"""
    action: Literal["BUY_LIMIT"] = Field(description="BUY_LIMIT: 限价买入现货")
    entry_price: float = Field(description="入场的价格（限价单）")
    amount: float = Field(description="下单数量 (币种数量)")
    reason: str = Field(description="定投买入理由")

class OpenOrderStrategy(BaseModel):
    """策略模式开仓参数：包含止盈止损和有效期"""
    action: Literal["BUY_LIMIT", "SELL_LIMIT"] = Field(description="BUY_LIMIT: 限价开多, SELL_LIMIT: 限价开空")
    entry_price: float = Field(description="入场的价格")
    amount: float = Field(description="下单数量 (币种数量)")
    take_profit: Optional[float] = Field(None, description="计划止盈价格")
    stop_loss: Optional[float] = Field(None, description="计划止损价格")
    valid_duration_hours: int = Field(24, description="挂单有效期(小时)，过期自动撤销")
    reason: str = Field(description="开仓理由")

class CloseOrder(BaseModel):
    """平仓的精确参数"""
    action: Literal["CLOSE"] = Field("CLOSE", description="固定为 CLOSE")
    pos_side: Literal["LONG", "SHORT"] = Field(description="你要平掉哪一个方向的仓位: LONG(平多), SHORT(平空)")
    entry_price: float = Field(description="触发平仓的价格，自动挂上限价订单")
    amount: float = Field(description="数量 (币种数量)")
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
    screener_result: Optional[Dict[str, Any]] = None
