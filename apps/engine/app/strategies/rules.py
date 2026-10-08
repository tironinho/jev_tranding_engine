from __future__ import annotations

from app.config import CombinationConfig
from app.domain.enums import (
    JEV_FALSE_BREAKOUT,
    JEV_HIGH_REVERSAL,
    JEV_LOW_CONTINUATION,
    OPENAI_ANOMALY,
    OPENAI_LOW_CONFIDENCE,
    OPENAI_REGIME_BLOCK,
    Action,
)
from app.domain.mathutil import clip
from app.providers.jev.schemas import JevAssessment
from app.providers.openai.schemas import MarketState


def meta_hit_probability(rr: float, round_trip_fee: float, stop_pct: float) -> float:
    """Chance the target must have, before costs, for the bet to be worth taking.

    p > (loss + cost) / (gain + loss), with gain and loss measured in R.
    """
    if rr <= 0 or stop_pct <= 0:
        return 1.0
    cost_r = max(round_trip_fee, 0.0) / stop_pct
    return (1.0 + cost_r) / (rr + 1.0)


def apply_jev_veto(
    action: Action,
    confidence: float,
    assessment: JevAssessment,
    config: CombinationConfig,
    breakout: bool,
    min_continuation: float | None = None,
) -> tuple[Action, float, list[str]]:
    """Veto-only v1. Jev cannot create or flip a trade."""
    reasons: list[str] = []
    if min_continuation is None:
        required = config.min_short_continuation if action is Action.SHORT else config.min_trend_continuation
    else:
        required = min_continuation
    if assessment.trend_continuation_probability < required:
        reasons.append(JEV_LOW_CONTINUATION)
    if assessment.reversal_probability > config.max_reversal:
        reasons.append(JEV_HIGH_REVERSAL)
    if breakout and assessment.false_breakout_probability > config.max_false_breakout:
        reasons.append(JEV_FALSE_BREAKOUT)
    if reasons:
        return Action.NO_TRADE, confidence, reasons
    blended = clip(
        config.baseline_weight * confidence + config.jev_weight * assessment.trend_continuation_probability,
        0,
        1,
    )
    return action, blended, ["JEV_CONFIRM"]


def apply_openai_veto(action: Action, state: MarketState, config: CombinationConfig) -> list[str]:
    reasons: list[str] = []
    if state.confidence < config.min_openai_confidence:
        reasons.append(OPENAI_LOW_CONFIDENCE)
    if state.anomaly_score > config.max_anomaly:
        reasons.append(OPENAI_ANOMALY)
    if state.reversal_risk > config.max_reversal_risk:
        reasons.append(OPENAI_REGIME_BLOCK)
    if action is Action.LONG and state.market_regime in config.block_long_regimes:
        reasons.append(OPENAI_REGIME_BLOCK)
    if action is Action.SHORT and state.market_regime in config.block_short_regimes:
        reasons.append(OPENAI_REGIME_BLOCK)
    return reasons
