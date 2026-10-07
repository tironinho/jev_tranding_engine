from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MarketRegime = Literal[
    "bullish_expansion",
    "bullish_exhaustion",
    "bearish_expansion",
    "bearish_exhaustion",
    "range",
    "high_volatility_chop",
    "breakout",
    "breakdown",
    "unclear",
]

OrderflowState = Literal[
    "aggressive_buying",
    "aggressive_selling",
    "balanced",
    "absorption",
    "unclear",
]

LiquidityState = Literal["bid_heavy", "ask_heavy", "balanced", "thin", "unclear"]


class MarketState(BaseModel):
    market_regime: MarketRegime
    trend_strength: float = Field(ge=0, le=1)
    momentum_quality: float = Field(ge=0, le=1)
    orderflow_state: OrderflowState
    liquidity_state: LiquidityState
    breakout_quality: float = Field(ge=0, le=1)
    overextension: float = Field(ge=0, le=1)
    reversal_risk: float = Field(ge=0, le=1)
    anomaly_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=8)

    def evidence_is_bounded(self) -> bool:
        return all(0 < len(item) <= 160 for item in self.evidence)


MARKET_STATE_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "market_regime": {
            "type": "string",
            "enum": [
                "bullish_expansion",
                "bullish_exhaustion",
                "bearish_expansion",
                "bearish_exhaustion",
                "range",
                "high_volatility_chop",
                "breakout",
                "breakdown",
                "unclear",
            ],
        },
        "trend_strength": {"type": "number"},
        "momentum_quality": {"type": "number"},
        "orderflow_state": {
            "type": "string",
            "enum": ["aggressive_buying", "aggressive_selling", "balanced", "absorption", "unclear"],
        },
        "liquidity_state": {
            "type": "string",
            "enum": ["bid_heavy", "ask_heavy", "balanced", "thin", "unclear"],
        },
        "breakout_quality": {"type": "number"},
        "overextension": {"type": "number"},
        "reversal_risk": {"type": "number"},
        "anomaly_score": {"type": "number"},
        "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
    },
    "required": [
        "market_regime",
        "trend_strength",
        "momentum_quality",
        "orderflow_state",
        "liquidity_state",
        "breakout_quality",
        "overextension",
        "reversal_risk",
        "anomaly_score",
        "confidence",
        "evidence",
    ],
}


class OpenAINotConfigured(Exception):
    pass


class OpenAIInvalidSchema(Exception):
    pass


class OpenAICallError(Exception):
    pass
