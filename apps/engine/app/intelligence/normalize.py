from __future__ import annotations

import math

from app.domain.mathutil import clip
from app.features.indicators import percentile_rank
from app.intelligence.schemas import Direction

_MAD_SCALE = 0.6745
_DIRECTION_BAND = 0.2
_STRONG_BAND = 0.6


def robust_zscore(value: float | None, history: list[float]) -> float | None:
    """0.6745 * (x - median) / MAD. A zero MAD does not invent an infinite score."""
    if value is None or not math.isfinite(value):
        return None
    sample = [item for item in history if math.isfinite(item)]
    if not sample:
        return None
    ordered = sorted(sample)
    median = _median(ordered)
    deviations = sorted(abs(item - median) for item in ordered)
    mad = _median(deviations)
    if mad <= 1e-12:
        return 0.0 if abs(value - median) <= 1e-12 else None
    score = _MAD_SCALE * (value - median) / mad
    if not math.isfinite(score):
        return None
    return score


def signed_from_z(zscore: float | None) -> float | None:
    if zscore is None or not math.isfinite(zscore):
        return None
    return math.tanh(zscore / 3.0)


def percentile_signed(history: list[float], value: float | None) -> tuple[float | None, float | None]:
    if value is None or not math.isfinite(value):
        return None, None
    rank = percentile_rank(history, value)
    if rank is None:
        return None, None
    return rank, clip(2.0 * rank - 1.0, -1.0, 1.0)


def direction_of(normalized: float | None) -> Direction:
    if normalized is None or not math.isfinite(normalized):
        return "UNKNOWN"
    if normalized <= -_STRONG_BAND:
        return "STRONGLY_NEGATIVE"
    if normalized < -_DIRECTION_BAND:
        return "NEGATIVE"
    if normalized <= _DIRECTION_BAND:
        return "NEUTRAL"
    if normalized < _STRONG_BAND:
        return "POSITIVE"
    return "STRONGLY_POSITIVE"


def fear_greed_signed(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return clip((value - 50.0) / 50.0, -1.0, 1.0)


def oi_log_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if current <= 0 or previous <= 0 or not math.isfinite(current) or not math.isfinite(previous):
        return None
    change = math.log(current / previous)
    return change if math.isfinite(change) else None


def funding_bps(rate: float | None) -> float | None:
    if rate is None or not math.isfinite(rate):
        return None
    return rate * 10_000.0


def log_ratio(ratio: float | None) -> float | None:
    if ratio is None or ratio <= 0 or not math.isfinite(ratio):
        return None
    value = math.log(ratio)
    return value if math.isfinite(value) else None


def liquidation_imbalance(long_liq: float | None, short_liq: float | None, epsilon: float = 1e-12) -> float | None:
    if long_liq is None or short_liq is None:
        return None
    if long_liq < 0 or short_liq < 0 or not math.isfinite(long_liq) or not math.isfinite(short_liq):
        return None
    return (short_liq - long_liq) / (long_liq + short_liq + epsilon)


def taker_imbalance(buy: float | None, sell: float | None, epsilon: float = 1e-12) -> float | None:
    if buy is None or sell is None:
        return None
    if not math.isfinite(buy) or not math.isfinite(sell):
        return None
    return (buy - sell) / (buy + sell + epsilon)


def book_imbalance(bid_depth: float | None, ask_depth: float | None) -> float | None:
    if bid_depth is None or ask_depth is None:
        return None
    if not math.isfinite(bid_depth) or not math.isfinite(ask_depth):
        return None
    denom = bid_depth + ask_depth
    if denom == 0:
        return None
    return (bid_depth - ask_depth) / denom


def spread_bps(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    if best_ask < best_bid or not math.isfinite(best_bid) or not math.isfinite(best_ask):
        return None
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    return (best_ask - best_bid) / mid * 10_000.0


def _median(ordered: list[float]) -> float:
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0
