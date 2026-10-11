"""Read-only Binance market analyst.

This module turns the public market features already collected by the engine
into a compact, auditable context.  It has no credentials, network client, or
order methods and cannot authorize a trade.
"""
from __future__ import annotations

import math
from statistics import fmean


VERSION = "binance_agent_context_v1"


def build_binance_agent_context(features: dict) -> dict:
    trend_inputs = (
        _finite(features.get("ema_alignment")),
        _scaled(features.get("return_15m"), 0.01),
        _scaled(features.get("context_15m_slope"), 0.005),
        _scaled(features.get("price_vs_ema20_15m"), 0.01),
    )
    momentum_inputs = (
        _scaled(features.get("roc"), 0.004),
        _scaled(features.get("return_5m"), 0.005),
        _centered_rsi(features.get("rsi")),
    )
    flow_inputs = (
        _finite(features.get("taker_flow_1m")),
        _finite(features.get("taker_flow_5m")),
        _finite(features.get("orderflow_delta_ratio")),
        _finite(features.get("imbalance_5")),
        _finite(features.get("imbalance_10")),
        _finite(features.get("imbalance_20")),
    )
    trend = _mean(trend_inputs)
    momentum = _mean(momentum_inputs)
    flow = _mean(flow_inputs)
    components = [item for item in (trend, momentum, flow) if item is not None]
    directional_bias = _weighted_bias(trend, momentum, flow)
    spread = _non_negative(features.get("spread_bps"))
    volume_ratio = _non_negative(features.get("volume_ratio"))
    atr = _non_negative(features.get("atr_normalized"))
    risks: list[str] = []
    if spread is not None and spread > 8:
        risks.append("WIDE_SPREAD")
    if volume_ratio is not None and volume_ratio < 0.4:
        risks.append("LOW_RELATIVE_VOLUME")
    if atr is not None and atr >= 0.02:
        risks.append("HIGH_VOLATILITY")
    expected = len(trend_inputs) + len(momentum_inputs) + len(flow_inputs) + 3
    available = sum(item is not None for item in (*trend_inputs, *momentum_inputs, *flow_inputs, spread, volume_ratio, atr))
    quality = available / expected
    state = "INSUFFICIENT_DATA"
    if len(components) >= 2 and quality >= 0.45:
        state = "BULLISH" if directional_bias > 0.20 else "BEARISH" if directional_bias < -0.20 else "NEUTRAL"
    return {
        "version": VERSION,
        "mode": "READ_ONLY_SHADOW",
        "source": "binance_public_market_data",
        "can_execute": False,
        "state": state,
        "directional_bias": directional_bias,
        "components": {"trend": trend, "momentum": momentum, "orderflow": flow},
        "liquidity": {"spread_bps": spread, "volume_ratio": volume_ratio},
        "volatility": {"atr_normalized": atr},
        "risk_flags": risks,
        "quality": quality,
    }


def _weighted_bias(trend: float | None, momentum: float | None, flow: float | None) -> float:
    present = [(trend, 0.40), (momentum, 0.25), (flow, 0.35)]
    usable = [(value, weight) for value, weight in present if value is not None]
    if not usable:
        return 0.0
    total = sum(weight for _, weight in usable)
    return max(-1.0, min(1.0, sum(value * weight for value, weight in usable) / total))


def _mean(values) -> float | None:
    present = [value for value in values if value is not None]
    return fmean(present) if present else None


def _scaled(value: object, scale: float) -> float | None:
    number = _finite(value)
    return math.tanh(number / scale) if number is not None and scale > 0 else None


def _centered_rsi(value: object) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    return max(-1.0, min(1.0, (number - 50.0) / 25.0))


def _non_negative(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
