from __future__ import annotations

from dataclasses import dataclass

from app.config import BaselineWeightConfig
from app.domain.enums import (
    ABOVE_VWAP,
    BAD_SPREAD,
    BELOW_THRESHOLD,
    BELOW_VWAP,
    BREAKDOWN,
    BREAKOUT,
    CLASS_ALIGNED,
    CLASS_DIVERGENT,
    CLASS_FLOW_AGAINST,
    CLASS_RANGE,
    CLASS_SINGLE_DRIVER,
    HIGH_VOLUME,
    HIGH_VOLATILITY,
    INSUFFICIENT_HISTORY,
    LOW_LIQUIDITY,
    NEGATIVE_CVD,
    POSITIVE_CVD,
    STALE_MARKET_DATA,
    STRONG_ASK_IMBALANCE,
    STRONG_BID_IMBALANCE,
    TREND_DOWN,
    TREND_UP,
    Action,
)
from app.domain.mathutil import clip
from app.domain.schemas import MarketSnapshot


# A component below this magnitude does not vote. It is a scale gate, not a fitted edge.
_COMPONENT_FLOOR = 0.15
_AGREEMENT_ALIGNED = 0.75
_DIRECTIONAL = (
    ("trend_score", "trend"),
    ("momentum_score", "momentum"),
    ("volume_score", "volume"),
    ("orderflow_score", "orderflow"),
    ("structure_score", "structure"),
)
_REFUSAL = (CLASS_DIVERGENT, CLASS_SINGLE_DRIVER, CLASS_FLOW_AGAINST)


@dataclass(frozen=True)
class MarketClass:
    primary: str
    labels: tuple[str, ...]
    agreement: float
    drivers: int


_UNAVAILABLE = MarketClass("unavailable", (), 0.0, 0)


@dataclass(frozen=True)
class BaselineResult:
    action: Action
    confidence: float
    composite: float
    scores: dict[str, float | None]
    reason_codes: list[str]
    version: str
    market_class: MarketClass = _UNAVAILABLE


def _num(features: dict, name: str) -> float | None:
    value = features.get(name)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def score_components(snapshot: MarketSnapshot, config: BaselineWeightConfig) -> dict[str, float | None] | None:
    """Map explainable features into [-1, 1]. Scale constants are not fitted edges."""
    features = snapshot.features
    alignment = _num(features, "ema_alignment")
    slope = _num(features, "ema_20_slope")
    price_vs = _num(features, "price_vs_ema20")
    rsi_value = _num(features, "rsi")
    roc_value = _num(features, "roc")
    volume_ratio = _num(features, "volume_ratio")
    volume_z = _num(features, "volume_zscore")
    delta_ratio = _num(features, "orderflow_delta_ratio")
    imbalance = _num(features, "imbalance_10")
    range_pos = _num(features, "range_position")
    atr_norm = _num(features, "atr_normalized")
    spread_bps = _num(features, "spread_bps")
    # Spread and volume can be missing for a minute. Candles are the history.
    if None in (alignment, rsi_value, atr_norm):
        return None

    slope_n = clip((slope or 0.0) / config.slope_scale, -1, 1)
    extension_n = clip((price_vs or 0.0) / config.price_vs_ema_scale, -1, 1)
    hour = _num(features, "return_60m")
    if hour is None:
        local_trend = clip(0.5 * alignment + 0.3 * slope_n + 0.2 * extension_n, -1, 1)
    else:
        hour_n = clip(hour / config.return_60m_scale, -1, 1)
        local_trend = clip(0.4 * alignment + 0.25 * slope_n + 0.15 * extension_n + 0.2 * hour_n, -1, 1)

    context_parts = (
        (0.5, _scaled(features, "context_15m_slope", config.context_15m_slope_scale)),
        (0.3, _scaled(features, "price_vs_ema20_15m", config.context_15m_distance_scale)),
        (0.2, _scaled(features, "setup_5m_return", config.setup_5m_return_scale)),
    )
    available_context = [(weight, value) for weight, value in context_parts if value is not None]
    context_trend = (
        sum(weight * value for weight, value in available_context) / sum(weight for weight, _ in available_context)
        if available_context
        else None
    )
    rsi_n = clip((rsi_value - 50) / 20, -1, 1)
    roc_n = clip((roc_value or 0.0) / config.roc_scale, -1, 1)
    momentum = clip(0.6 * rsi_n + 0.4 * roc_n, -1, 1)

    taker_flow = _num(features, "taker_flow_1m")
    flow = taker_flow if taker_flow is not None else delta_ratio
    if volume_ratio is None or flow is None:
        volume = None
        orderflow = None
    else:
        # Below-average activity withholds confirmation; it must not manufacture
        # a directional vote opposite to the observed taker flow.
        volume_mag = clip((volume_ratio - 1) / 1.5, 0, 1)
        volume = clip(volume_mag * (1 if flow >= 0 else -1), -1, 1)
        imb = imbalance if imbalance is not None else 0.0
        orderflow = clip(0.7 * flow + 0.3 * imb, -1, 1)

    # A closed 15m EMA is deliberately slow. During a fresh, confirmed break it
    # describes the regime we are leaving and must not veto the new direction.
    # Require both momentum and live order flow to agree with the structural
    # event before neutralising lagging votes. The break is exposed separately
    # to JEV; it does not manufacture enough score to cross the baseline gate.
    break_side = 1 if features.get("breakout") is True else -1 if features.get("breakdown") is True else 0
    break_confirmed = bool(
        break_side
        and momentum * break_side >= _COMPONENT_FLOOR
        and orderflow is not None
        and orderflow * break_side >= _COMPONENT_FLOOR
    )
    if break_confirmed:
        local_same_side = local_trend if local_trend * break_side > 0 else 0.0
        context_same_side = context_trend if context_trend is not None and context_trend * break_side > 0 else 0.0
        trend = clip(0.80 * local_same_side + 0.20 * context_same_side, -1, 1)
    else:
        trend = local_trend if context_trend is None else clip(0.65 * local_trend + 0.35 * context_trend, -1, 1)

    structure = 0.0
    if range_pos is not None:
        structure = clip((range_pos - 0.5) * 2, -1, 1)
    if features.get("breakout") is True:
        structure = clip(structure + 0.25, -1, 1)
    if features.get("breakdown") is True:
        structure = clip(structure - 0.25, -1, 1)

    if atr_norm >= config.extreme_atr_normalized:
        volatility = -1.0
    elif atr_norm >= config.high_atr_normalized:
        volatility = -0.4
    else:
        volatility = clip(1 - (atr_norm / config.high_atr_normalized), 0, 1)

    if spread_bps is None:
        liquidity = None
    elif spread_bps >= config.max_spread_bps:
        liquidity = -1.0
    else:
        liquidity = 0.0

    scores: dict[str, float | None] = {
        "trend_score": trend,
        "context_trend_score": context_trend,
        "break_confirmation_score": float(break_side) if break_confirmed else 0.0,
        "momentum_score": momentum,
        "volume_score": volume,
        "orderflow_score": orderflow,
        "structure_score": structure,
        "volatility_score": volatility,
        "liquidity_score": liquidity,
    }
    return scores


def _scaled(features: dict, name: str, scale: float) -> float | None:
    value = _num(features, name)
    if value is None or scale <= 0:
        return None
    return clip(value / scale, -1, 1)


def classify_scores(
    scores: dict[str, float | None],
    features: dict,
    composite: float,
    config: BaselineWeightConfig,
) -> MarketClass:
    """Name the situation from the weighted scores. This is not an order."""
    side = 1 if composite > 0 else -1 if composite < 0 else 0
    agree_weight = 0.0
    total_weight = 0.0
    drivers = 0
    for name, attr in _DIRECTIONAL:
        value = scores.get(name)
        weight = getattr(config, attr)
        if value is None or weight <= 0 or abs(value) < _COMPONENT_FLOOR:
            continue
        drivers += 1
        total_weight += weight
        if side and (value > 0) == (side > 0):
            agree_weight += weight
    agreement = agree_weight / total_weight if total_weight else 0.0

    labels: list[str] = []
    if drivers >= 2 and agreement >= _AGREEMENT_ALIGNED:
        labels.append(CLASS_ALIGNED)
    elif drivers == 1:
        labels.append(CLASS_SINGLE_DRIVER)
    elif drivers >= 2:
        labels.append(CLASS_DIVERGENT)

    flow = scores.get("orderflow_score")
    if side and flow is not None and abs(flow) >= _COMPONENT_FLOOR and (flow > 0) != (side > 0):
        labels.append(CLASS_FLOW_AGAINST)

    if features.get("breakout") is True:
        labels.append(BREAKOUT)
    elif features.get("breakdown") is True:
        labels.append(BREAKDOWN)
    else:
        structure = scores.get("structure_score")
        if structure is None or abs(structure) < _COMPONENT_FLOOR:
            labels.append(CLASS_RANGE)

    volatility = scores.get("volatility_score")
    if volatility is not None and volatility <= -1.0:
        labels.append(HIGH_VOLATILITY)

    return MarketClass(_primary(labels), tuple(labels), agreement, drivers)


def _primary(labels: list[str]) -> str:
    for code in (
        HIGH_VOLATILITY,
        CLASS_FLOW_AGAINST,
        CLASS_DIVERGENT,
        CLASS_SINGLE_DRIVER,
        CLASS_ALIGNED,
        BREAKOUT,
        BREAKDOWN,
        CLASS_RANGE,
    ):
        if code in labels:
            return code
    return "unclear"


def combine_scores(scores: dict[str, float | None], config: BaselineWeightConfig) -> float:
    """Missing flow components are left out. Their weight is not given to the rest."""
    terms = (
        (config.trend, scores.get("trend_score")),
        (config.momentum, scores.get("momentum_score")),
        (config.volume, scores.get("volume_score")),
        (config.orderflow, scores.get("orderflow_score")),
        (config.structure, scores.get("structure_score")),
        (config.volatility, scores.get("volatility_score")),
        (config.liquidity, scores.get("liquidity_score")),
    )
    return sum(weight * value for weight, value in terms if value is not None)


def score_baseline(snapshot: MarketSnapshot, config: BaselineWeightConfig) -> BaselineResult:
    if snapshot.data_quality.stale:
        return BaselineResult(Action.NO_TRADE, 0.0, 0.0, {}, [STALE_MARKET_DATA], config.version)
    scores = score_components(snapshot, config)
    if scores is None:
        missing = [
            name
            for name in ("ema_alignment", "rsi", "atr_normalized")
            if snapshot.features.get(name) is None
        ]
        return BaselineResult(
            Action.NO_TRADE,
            0.0,
            0.0,
            {},
            [INSUFFICIENT_HISTORY, *missing],
            config.version,
        )

    features = snapshot.features
    composite = clip(combine_scores(scores, config), -1, 1)
    market_class = classify_scores(scores, features, composite, config)
    reasons = list(market_class.labels)
    spread_bps = float(features.get("spread_bps") or 0)
    if spread_bps > config.max_spread_bps:
        reasons.append(BAD_SPREAD)
    if (scores.get("liquidity_score") or 0) < 0:
        reasons.append(LOW_LIQUIDITY)
    atr_norm = float(features.get("atr_normalized") or 0)
    if atr_norm >= config.extreme_atr_normalized:
        reasons.append(HIGH_VOLATILITY)
    volume_ratio = features.get("volume_ratio")
    if isinstance(volume_ratio, (int, float)) and volume_ratio < config.min_volume_ratio:
        reasons.append(LOW_LIQUIDITY)

    hard_block = {BAD_SPREAD, HIGH_VOLATILITY}
    refused = any(code in _REFUSAL for code in market_class.labels)
    if any(code in hard_block for code in reasons) or refused or abs(composite) < config.min_abs_score:
        if abs(composite) < config.min_abs_score:
            reasons.append(BELOW_THRESHOLD)
        return BaselineResult(
            Action.NO_TRADE,
            abs(composite),
            composite,
            scores,
            _unique(reasons),
            config.version,
            market_class,
        )

    action = Action.LONG if composite > 0 else Action.SHORT
    reasons.extend(_context_reasons(features, action, config))
    return BaselineResult(
        action,
        abs(composite),
        composite,
        scores,
        _unique(reasons),
        config.version,
        market_class,
    )


def _context_reasons(features: dict, action: Action, config: BaselineWeightConfig) -> list[str]:
    reasons: list[str] = []
    if action is Action.LONG:
        reasons.append(TREND_UP)
    else:
        reasons.append(TREND_DOWN)
    distance = features.get("distance_to_vwap")
    if isinstance(distance, (int, float)):
        if distance > 0:
            reasons.append(ABOVE_VWAP)
        elif distance < 0:
            reasons.append(BELOW_VWAP)
    cvd = features.get("cvd_slope")
    if isinstance(cvd, (int, float)):
        reasons.append(POSITIVE_CVD if cvd > 0 else NEGATIVE_CVD)
    imbalance = features.get("imbalance_10")
    if isinstance(imbalance, (int, float)) and abs(imbalance) >= config.imbalance_strong:
        reasons.append(STRONG_BID_IMBALANCE if imbalance > 0 else STRONG_ASK_IMBALANCE)
    volume_z = features.get("volume_zscore")
    if isinstance(volume_z, (int, float)) and volume_z >= config.volume_z_strong:
        reasons.append(HIGH_VOLUME)
    if features.get("breakout") is True:
        reasons.append(BREAKOUT)
    if features.get("breakdown") is True:
        reasons.append(BREAKDOWN)
    return reasons


def _unique(reasons: list[str]) -> list[str]:
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return seen
