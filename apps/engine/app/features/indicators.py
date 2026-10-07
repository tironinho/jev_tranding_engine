from __future__ import annotations

import math


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    seed = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    value = seed
    for price in values[period:]:
        value = price * alpha + value * (1 - alpha)
    return value


def ema_slope(values: list[float], period: int, lookback: int = 5) -> float | None:
    if len(values) < period + lookback:
        return None
    current = ema(values, period)
    previous = ema(values[:-lookback], period)
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def roc(values: list[float], period: int = 10) -> float | None:
    if len(values) <= period or values[-period - 1] == 0:
        return None
    return values[-1] / values[-period - 1] - 1


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    ranges: list[float] = []
    for index in range(1, len(closes)):
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return ranges


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    ranges = true_ranges(highs, lows, closes)
    if len(ranges) < period:
        return None
    value = sum(ranges[:period]) / period
    for item in ranges[period:]:
        value = (value * (period - 1) + item) / period
    return value


def realized_volatility(closes: list[float], window: int = 30) -> float | None:
    if len(closes) < window + 1:
        return None
    sample = closes[-(window + 1) :]
    rets: list[float] = []
    for index in range(1, len(sample)):
        if sample[index - 1] <= 0 or sample[index] <= 0:
            return None
        rets.append(math.log(sample[index] / sample[index - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    return math.sqrt(var)


def percentile_rank(history: list[float], current: float) -> float | None:
    if not history:
        return None
    less_or_equal = sum(1 for item in history if item <= current)
    return less_or_equal / len(history)


def vwap_from_candles(typical: list[float], volumes: list[float]) -> float | None:
    denominator = sum(volumes)
    if denominator == 0 or len(typical) != len(volumes) or not typical:
        return None
    return sum(price * volume for price, volume in zip(typical, volumes, strict=True)) / denominator


def zscore(current: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    mean = sum(history) / len(history)
    var = sum((item - mean) ** 2 for item in history) / (len(history) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (current - mean) / std
