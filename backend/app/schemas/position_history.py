"""Authenticated, nullable history response contracts."""
from typing import Literal

from pydantic import BaseModel, Field


class PositionEvent(BaseModel):
    event_id: str
    kind: str
    timestamp: str | None = None
    trade_id: str | None = None
    order_id: str | None = None
    quantity: float | None = None
    price: float | None = None
    quantity_before: float | None = None
    quantity_after: float | None = None
    realized_pnl_before_fees: float | None = None
    realized_net_pnl: float | None = None
    fees: list[dict] = Field(default_factory=list)
    source: str
    reason: str | None = None
    issues: list[str] = Field(default_factory=list)
    is_fill: bool


class PositionSummary(BaseModel):
    position_id: str
    config_id: str
    mode: str
    symbol: str
    side: str
    status: Literal['open', 'closed', 'unconfirmed']
    opened_at: str | None
    closed_at: str | None
    last_event_at: str | None
    holding_seconds: float | None
    entered_quantity: float | None
    remaining_quantity: float | None
    realized_pnl_before_fees: float | None
    realized_net_pnl: float | None
    currency: str
    fees_by_currency: dict[str, float]
    complete: bool
    issues: list[str]


class PositionDetail(PositionSummary):
    events: list[PositionEvent]


class PositionPage(BaseModel):
    success: bool = True
    items: list[PositionSummary]
    total: int
    page: int
    per_page: int
    symbols: list[str]
    as_of: str
    complete: bool
    errors: dict[str, str]


class PositionDetailResponse(BaseModel):
    success: bool = True
    position: PositionDetail
    as_of: str
