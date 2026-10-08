from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class JevMarketRequest(BaseModel):
    prompt_version: str
    symbol: str
    market_type: str
    timestamp: datetime
    features: dict[str, Any]
    baseline_action: str | None = None
    baseline_confidence: float | None = None
    baseline_scores: dict[str, float | None] | None = None
    market_state: dict[str, Any] | None = None
    intelligence: dict[str, Any] | None = None
    questions: list[str] = Field(
        default_factory=lambda: [
            "trend_continuation_probability",
            "reversal_probability",
            "false_breakout_probability",
        ]
    )


class JevAssessment(BaseModel):
    provider: Literal["mock", "real"]
    is_mock: bool
    provider_version: str
    prompt_version: str
    model: str | None = None
    trend_continuation_probability: float
    reversal_probability: float
    false_breakout_probability: float
    buying_pressure_probability: float = 0.5
    selling_pressure_probability: float = 0.5
    volatility_expansion_probability: float = 0.5
    liquidity_sweep_probability: float = 0.5
    latency_ms: float | None = None
    error: str | None = None

    @field_validator(
        "trend_continuation_probability",
        "reversal_probability",
        "buying_pressure_probability",
        "selling_pressure_probability",
        "false_breakout_probability",
        "volatility_expansion_probability",
        "liquidity_sweep_probability",
    )
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("probability out of range")
        return value


class JevNotImplemented(Exception):
    """Raised by the real adapter until official documentation exists."""


class JevProviderError(Exception):
    pass
