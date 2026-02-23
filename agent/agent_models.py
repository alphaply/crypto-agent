from typing import Any, Dict, List, Literal, Optional

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
    market_sentiment: str = Field(description="当前市场的趋势与动能分析")
    # timeframe_alignment: str = Field(description="对多个周期进行趋势分析")
    key_levels: str = Field(description="根据周期，数据指标分析的支撑位和阻力位")
    strategy_logic: str = Field(description="存到历史记录的文字内容，做单的逻辑等等")
    prediction: str = Field(description="对市场进行一个大致预测")


class RealAgentOutput(BaseModel):
    summary: RealMarketSummary
    orders: List[RealOrderParams]


class StrategyOrderParams(BaseModel):
    reason: str = Field(description="策略逻辑与盈亏比分析")
    action: Literal["BUY_LIMIT", "SELL_LIMIT", "CANCEL", "NO_ACTION"] = Field(description="策略动作")
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="入场挂单价格", default=0.0)
    amount: float = Field(description="模拟下单数量", default=0.0)
    take_profit: float = Field(description="计划止盈位(必须设置)", default=0.0)
    stop_loss: float = Field(description="计划止损位(必须设置)", default=0.0)
    valid_duration_hours: int = Field(description="挂单有效期(小时)", default=24)


class StrategyMarketSummary(BaseModel):
    
    market_sentiment: str = Field(description="当前市场的趋势与动能分析")
    key_levels: str = Field(description="根据周期，数据指标找到支撑位和阻力位")
    strategy_logic: str = Field(description="存到历史记录的文字内容，做单的逻辑等等")
    prediction: str = Field(description="对市场进行一个预测")
    # market_sentiment: str = Field(description="综合资金费率、成交量和 OI，判断当前是贪婪、恐惧还是观望。")
    # timeframe_alignment: str = Field(description="4h, 1h, 15m 的趋势是否统一？若冲突，目前应采取何种策略？")
    # key_levels: str = Field(description="识别关键的供需区 (Supply/Demand) 和流动性池。")
    # strategy_logic: str = Field(description="详细的策略思维链、盈亏比逻辑与挂单失效条件。")
    # risk_reward_ratio: str = Field(description="预估这笔交易的盈亏比。")


class StrategyAgentOutput(BaseModel):
    summary: StrategyMarketSummary
    orders: List[StrategyOrderParams]


class AgentState(BaseModel):
    config_id: str
    symbol: str
    messages: List[BaseMessage]
    agent_config: Dict[str, Any]
    market_context: Dict[str, Any]
    account_context: Dict[str, Any]
    history_context: List[Dict[str, Any]]
    final_output: Dict[str, Any]
