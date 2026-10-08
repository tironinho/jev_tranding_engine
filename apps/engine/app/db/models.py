from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> uuid.UUID:
    return uuid.uuid4()


class SymbolRow(Base):
    __tablename__ = "symbols"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    market_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    market_type: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB)


class FeatureSnapshotRow(Base):
    __tablename__ = "feature_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("market_snapshots.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class StrategyConfigRow(Base):
    __tablename__ = "strategy_configs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy_key: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[str] = mapped_column(String(16))
    config: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyDecisionRow(Base):
    __tablename__ = "strategy_decisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("market_snapshots.id"), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(16))
    signal_status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)


class OpenAICallRow(Base):
    __tablename__ = "openai_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True, nullable=True)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class JevCallRow(Base):
    __tablename__ = "jev_calls"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True, nullable=True)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    provider_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategy_decisions.id"), index=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict] = mapped_column(JSONB)


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orders.id"), index=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB)


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB)


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    net_pnl: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quantitative_regime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class PaperAccountRow(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy: Mapped[str] = mapped_column(String(64), unique=True)
    cash: Mapped[float] = mapped_column(Float)
    day_start_equity: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSONB)


class PaperBalanceRow(Base):
    __tablename__ = "paper_balances"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    equity: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class PerformanceSnapshotRow(Base):
    __tablename__ = "performance_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ConsensusRow(Base):
    __tablename__ = "consensus_results"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    label: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)


class FutureLabelRow(Base):
    __tablename__ = "future_labels"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class AccountSampleRow(Base):
    """One marked reading of the real margin account. The curve is a view of this table."""

    __tablename__ = "account_samples"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[float] = mapped_column(Float)
    wallet: Mapped[float | None] = mapped_column(Float, nullable=True)
    available: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class EngineEventRow(Base):
    __tablename__ = "engine_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class StrategyVersionRow(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    strategy_family: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    parent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    experiment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class ExperimentRow(Base):
    __tablename__ = "experiments"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    strategy_family: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    parent_version: Mapped[str] = mapped_column(String(32))
    challenger_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hypothesis_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ExperimentHypothesisRow(Base):
    __tablename__ = "experiment_hypotheses"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class ExperimentMetricRow(Base):
    __tablename__ = "experiment_metrics"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)


class ExperimentRunRow(Base):
    __tablename__ = "experiment_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ExperimentArtifactRow(Base):
    __tablename__ = "experiment_artifacts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)


class AnomalyRow(Base):
    __tablename__ = "anomalies"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    strategy_family: Mapped[str] = mapped_column(String(64), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ResearchReportRow(Base):
    __tablename__ = "research_reports"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    decision: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class PromotionEventRow(Base):
    __tablename__ = "promotion_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy_family: Mapped[str] = mapped_column(String(64), index=True)
    from_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class StrategyDecayRow(Base):
    __tablename__ = "strategy_decay"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    strategy_family: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class CodingAgentTaskRow(Base):
    __tablename__ = "coding_agent_tasks"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
