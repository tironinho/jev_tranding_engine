from __future__ import annotations

from app.config import CombinationConfig
from app.domain.enums import (
    JEV_FALSE_BREAKOUT,
    JEV_HIGH_REVERSAL,
    JEV_LIQUIDITY_SWEEP,
    JEV_LOW_CONTINUATION,
    JEV_PRESSURE_DISAGREES,
    OPENAI_ANOMALY,
    OPENAI_LOW_CONFIDENCE,
    OPENAI_REGIME_BLOCK,
    Action,
)
from app.domain.mathutil import clip
from app.providers.jev.schemas import JevAssessment
from app.providers.openai.schemas import MarketState


def apply_jev_veto(
    action: Action,
    confidence: float,
    assessment: JevAssessment,
    config: CombinationConfig,
    breakout: bool,
) -> tuple[Action, float, list[str]]:
    """Veto-only v1. Jev cannot create or flip a trade."""
    reasons: list[str] = []
    if assessment.trend_continuation_probability < config.min_trend_continuation:
        reasons.append(JEV_LOW_CONTINUATION)
    if assessment.reversal_probability > config.max_reversal:
        reasons.append(JEV_HIGH_REVERSAL)
    if breakout and assessment.false_breakout_probability > config.max_false_breakout:
        reasons.append(JEV_FALSE_BREAKOUT)
    if action is Action.LONG:
        edge = assessment.buying_pressure_probability - assessment.selling_pressure_probability
    else:
        edge = assessment.selling_pressure_probability - assessment.buying_pressure_probability
    if edge < config.min_pressure_edge:
        reasons.append(JEV_PRESSURE_DISAGREES)
    if breakout and assessment.liquidity_sweep_probability > config.max_liquidity_sweep:
        reasons.append(JEV_LIQUIDITY_SWEEP)
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
