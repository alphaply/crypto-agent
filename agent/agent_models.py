from typing import Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


class RealOrderParams(BaseModel):
    reason: str = Field(description="简短的执行理由")
    action: Literal["BUY_LIMIT", "SELL_LIMIT", "CLOSE", "CANCEL", "NO_ACTION"] = Field(
        description="BUY_LIMIT现价开多、SELL_LIMIT现价开空、CLOSE进行平多平空、NO_ACTION不做任何事情、CANCEL取消挂单"
    )
    pos_side: Optional[Literal["LONG", "SHORT"]] = Field(description="平仓方向: CLOSE时必填", default=None)
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="挂单价格/平仓价格", default=0.0)
    amount: float = Field(description="下单数量", default=0.0)


class RealMarketSummary(BaseModel):
    market_trend: str = Field(description="当前短期市场微观趋势与动能")
    key_levels: str = Field(description="日内关键支撑位与阻力位")
    strategy_logic: str = Field(description="存到历史记录的文字内容，作为下次行情分析的参考（简短）。")
    prediction: str = Field(description="短期价格行为(Price Action)预测")


class RealAgentOutput(BaseModel):
    summary: RealMarketSummary
    orders: List[RealOrderParams]


class StrategyOrderParams(BaseModel):
    reason: str = Field(description="策略逻辑与盈亏比分析 (例如 R/R: 3.2)")
    action: Literal["BUY_LIMIT", "SELL_LIMIT", "CANCEL", "NO_ACTION"] = Field(description="策略动作")
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="入场挂单价格", default=0.0)
    amount: float = Field(description="模拟下单数量", default=0.0)
    take_profit: float = Field(description="计划止盈位(必须设置)", default=0.0)
    stop_loss: float = Field(description="计划止损位(必须设置)", default=0.0)
    valid_duration_hours: int = Field(description="挂单有效期(小时)", default=24)


class StrategyMarketSummary(BaseModel):
    market_trend: str = Field(description="4H/1D 宏观趋势分析")
    key_levels: str = Field(description="市场结构(Structure)、供需区与流动性分布")
    strategy_logic: str = Field(description="详细的策略思维链、盈亏比逻辑与挂单失效条件")
    prediction: str = Field(description="未来走势推演与剧本规划")


class StrategyAgentOutput(BaseModel):
    summary: StrategyMarketSummary
    orders: List[StrategyOrderParams]


class AgentState(TypedDict):
    config_id: str
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    final_output: Dict[str, Any]
