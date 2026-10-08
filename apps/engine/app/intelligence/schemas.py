from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.intelligence import NORMALIZATION_VERSION

Direction = Literal[
    "STRONGLY_NEGATIVE",
    "NEGATIVE",
    "NEUTRAL",
    "POSITIVE",
    "STRONGLY_POSITIVE",
    "UNKNOWN",
]
Horizon = Literal["MICRO", "SHORT", "MEDIUM", "SLOW"]
ProviderStatus = Literal["ONLINE", "DEGRADED", "STALE", "OFFLINE", "DISABLED", "ERROR"]


class RawExternalObservation(BaseModel):
    observation_id: UUID = Field(default_factory=uuid4)
    provider: str
    metric: str
    symbol: str | None = None
    asset: str | None = None
    provider_symbol: str | None = None
    value: float | None = None
    unit: str
    observed_at: datetime
    received_at: datetime
    provider_timestamp: datetime | None = None
    source_frequency_seconds: int | None = None
    metadata: dict = Field(default_factory=dict)


class NormalizedFeature(BaseModel):
    name: str
    raw_value: float | None = None
    normalized: float | None = None
    directional: float | None = None
    intensity: float | None = None
    zscore: float | None = None
    percentile: float | None = None
    delta_1: float | None = None
    delta_short: float | None = None
    delta_medium: float | None = None
    direction: Direction = "UNKNOWN"
    quality: float = 0.0
    freshness: float = 0.0
    confidence: float = 0.0
    available: bool = False
    valid_for_decision: bool = False
    horizon: Horizon = "SHORT"
    source: str
    source_timestamp: datetime | None = None
    normalization_version: str = NORMALIZATION_VERSION


class ProviderHealth(BaseModel):
    provider: str
    status: ProviderStatus
    last_success: datetime | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    error_rate: float | None = None
    rate_limit_count: int = 0
    stale_metrics: list[str] = Field(default_factory=list)


class ProviderCapability(BaseModel):
    provider: str
    metric: str
    asset: str | None = None
    frequency: str | None = None
    available: bool = False
    plan_required: str | None = None
    last_checked_at: datetime | None = None


class IntelligenceQuality(BaseModel):
    overall: float = 0.0
    categories: dict[str, float] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    provider_disagreement: float | None = None


class MarketIntelligenceSnapshot(BaseModel):
    intelligence_snapshot_id: UUID = Field(default_factory=uuid4)
    market_snapshot_id: UUID
    symbol: str
    generated_at: datetime
    microstructure: dict = Field(default_factory=dict)
    derivatives: dict = Field(default_factory=dict)
    sentiment: dict = Field(default_factory=dict)
    relative_strength: dict = Field(default_factory=dict)
    onchain: dict = Field(default_factory=dict)
    macro: dict = Field(default_factory=dict)
    quality: IntelligenceQuality = Field(default_factory=IntelligenceQuality)
    normalization_version: str = NORMALIZATION_VERSION
