from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    Action,
    ConsensusLabel,
    MarketType,
    OperatingMode,
    OrderStatus,
    OrderType,
    PositionStatus,
    SignalStatus,
)


class DataQuality(BaseModel):
    model_config = ConfigDict(frozen=True)

    stale: bool = False
    staleness_ms: int | None = None
    missing_features: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    book_available: bool = False
    trades_available: bool = False
    candles_available: bool = False
    derivatives_available: bool = False


class MarketSnapshot(BaseModel):
    """Immutable market picture shared by every strategy for one opportunity."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    symbol: str
    market_type: MarketType
    raw_market_data_reference: str
    price: float
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    spread_bps: float | None = None
    features: dict[str, Any]
    data_quality: DataQuality
    latency_ms: float | None = None
    quantitative_regime: str = "unclear|unknown_volatility"
    trigger: str = "manual"


class StrategyDecision(BaseModel):
    decision_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID
    opportunity_id: UUID
    snapshot_id: UUID
    strategy: str
    symbol: str
    timestamp: datetime
    action: Action
    confidence: float
    reason_codes: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_version: str | None = None
    prompt_version: str | None = None
    decision_latency_ms: float | None = None
    signal_status: SignalStatus = SignalStatus.VALID
    mode: OperatingMode = OperatingMode.SHADOW


class BookLevel(BaseModel):
    price: float
    quantity: float


class ExecutionPreview(BaseModel):
    model: str
    expected_price: float
    estimated_fill_price: float
    slippage_bps: float
    fully_filled: bool
    filled_quantity: float
    warnings: list[str] = Field(default_factory=list)


class CostBreakdown(BaseModel):
    entry_fee: float
    exit_fee_at_target: float
    exit_fee_at_stop: float
    spread_cost: float
    slippage_cost: float
    funding_cashflow: float
    fee_rate_entry: float
    fee_rate_exit: float
    fee_source: str
    spread_included_in_fill: bool


class TradeEconomics(BaseModel):
    entry: float
    stop: float
    target: float
    quantity: float
    gross_risk: float
    gross_reward: float
    gross_rr: float | None
    net_risk: float
    net_reward: float
    net_rr: float | None
    costs: CostBreakdown


class RiskDecision(BaseModel):
    risk_id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    correlation_id: UUID
    accepted: bool
    reject_reasons: list[str] = Field(default_factory=list)
    economics: TradeEconomics | None = None
    side: Action | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OrderRecord(BaseModel):
    order_id: UUID = Field(default_factory=uuid4)
    client_order_id: str
    decision_id: UUID
    risk_id: UUID | None = None
    strategy: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: OrderType
    status: OrderStatus
    mode: Literal["paper", "live"]
    quantity: float
    filled_quantity: float = 0
    price: float | None = None
    expected_price: float | None = None
    average_fill_price: float | None = None
    created_at: datetime


class FillRecord(BaseModel):
    fill_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    decision_id: UUID
    price: float
    quantity: float
    fee: float
    slippage_bps: float
    liquidity: Literal["maker", "taker"]
    filled_at: datetime


class PositionRecord(BaseModel):
    position_id: UUID = Field(default_factory=uuid4)
    strategy: str
    symbol: str
    side: Action
    status: PositionStatus = PositionStatus.OPEN
    quantity: float
    entry_price: float
    stop: float
    target: float
    opened_at: datetime
    closed_at: datetime | None = None
    decision_id: UUID
    trade_id: UUID | None = None
    mfe: float = 0
    mae: float = 0
    entry_fee: float = 0
    initial_net_risk: float = 0
    exit_fee_rate: float = 0
    fee_source: str = "config"
    quantitative_regime: str | None = None
    market_regime: str | None = None
    mode: Literal["paper", "live"] = "paper"
    stop_client_order_id: str | None = None


class TradeRecord(BaseModel):
    trade_id: UUID = Field(default_factory=uuid4)
    strategy: str
    symbol: str
    side: Action
    quantity: float
    entry_price: float
    exit_price: float
    stop: float
    target: float
    opened_at: datetime
    closed_at: datetime
    gross_pnl: float
    fees: float
    slippage: float
    funding: float
    net_pnl: float
    r_multiple: float | None
    mfe: float
    mae: float
    exit_reason: str
    decision_id: UUID
    quantitative_regime: str | None = None
    market_regime: str | None = None


class ConsensusResult(BaseModel):
    opportunity_id: UUID
    snapshot_id: UUID
    symbol: str
    timestamp: datetime
    label: ConsensusLabel
    actions: dict[str, str]


class AuditRecord(BaseModel):
    audit_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime
    actor: str
    action: str
    payload: dict[str, Any]
