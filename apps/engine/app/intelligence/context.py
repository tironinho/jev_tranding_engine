from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.domain.mathutil import ensure_utc
from app.intelligence.asof import asof_backward
from app.intelligence.freshness import age_seconds, freshness
from app.intelligence.normalize import (
    fear_greed_signed,
    funding_bps,
    liquidation_imbalance,
    log_ratio,
    oi_log_change,
    percentile_signed,
    robust_zscore,
    signed_from_z,
    taker_imbalance,
)
from app.intelligence.quality import feature_quality, relative_disagreement
from app.intelligence.schemas import RawExternalObservation

_SLOW = 6 * 3600


def build_context(symbol: str, features: dict, rows: list[RawExternalObservation], now: datetime) -> dict:
    """Normalized jev_feature_set_v1. Missing stays null."""
    visible = [row for row in rows if ensure_utc(row.observed_at) <= ensure_utc(now)]
    micro = _micro(features)
    derivatives = _derivatives(symbol, visible, now)
    sentiment = _sentiment(visible, now)
    global_market = _dominance(visible, now, symbol)
    quality_rows = [micro["quality"], derivatives["quality"], sentiment["quality"]]
    present = [item for item in quality_rows if item is not None]
    overall = sum(present) / len(present) if present else 0.0
    missing = [
        name
        for name, value in {
            "derivatives.oi_change_5m": derivatives.get("oi_change_5m"),
            "derivatives.funding_crowding": derivatives.get("funding_crowding"),
            "derivatives.liquidation_imbalance": derivatives.get("liquidation_imbalance"),
            "sentiment.fear_greed": sentiment.get("fear_greed"),
            "onchain.exchange_flow_pressure": None,
        }.items()
        if value is None
    ]
    return {
        "feature_set_version": "jev_feature_set_v1",
        "symbol": symbol,
        "timestamp": ensure_utc(now).isoformat(),
        "microstructure": micro,
        "derivatives": derivatives,
        "sentiment": sentiment,
        "relative_strength": _relative(symbol, visible, now),
        "onchain": {"exchange_flow_pressure": None, "stablecoin_liquidity": None, "available": False, "quality": 0.0},
        "global": global_market,
        "data_quality": {
            "overall": overall,
            "categories": {
                "microstructure": micro["quality"],
                "derivatives": derivatives["quality"],
                "sentiment": sentiment["quality"],
                "onchain": 0.0,
            },
            "missing": missing,
            "stale": [],
            "provider_disagreement": derivatives.get("provider_disagreement"),
        },
    }


def _micro(features: dict) -> dict:
    book_5 = _finite(features.get("imbalance_5"))
    book_20 = _finite(features.get("imbalance_20"))
    taker = _finite(features.get("taker_flow_1m"))
    values = [item for item in (book_5, book_20, taker) if item is not None]
    return {
        "taker_imbalance_1m": taker,
        "taker_imbalance_5m": None,
        "book_imbalance_5": book_5,
        "book_imbalance_20": book_20,
        "book_imbalance_consistency": _consistency(book_5, _finite(features.get("imbalance_10")), book_20),
        "cvd_direction": None,
        "spread_bps": _finite(features.get("spread_bps")),
        "spread_stress": None,
        "quality": feature_quality(available=bool(values), fresh=1.0 if values else 0.0),
        "source": "binance_book",
    }


def _derivatives(symbol: str, rows: list[RawExternalObservation], now: datetime) -> dict:
    oi = _series(rows, "coinalyze", "open_interest_usd", symbol)
    if not oi:
        oi = _series(rows, "binance", "open_interest_value", symbol)
    changes = {
        "oi_change_5m": _log_shift(oi, 300),
        "oi_change_15m": _log_shift(oi, 900),
        "oi_change_1h": _log_shift(oi, 3600),
    }
    history = [value for _, value in oi]
    latest = history[-1] if history else None
    rank, _signed = percentile_signed(history[:-1], latest) if latest is not None else (None, None)
    z = robust_zscore(latest, history[:-1]) if latest is not None else None
    funding = _series(rows, "binance", "funding_rate", symbol) or _series(rows, "coinalyze", "funding_current", symbol)
    funding_latest = funding[-1][1] if funding else None
    funding_z = robust_zscore(funding_latest, [value for _, value in funding[:-1]]) if funding_latest is not None else None
    crowding = signed_from_z(funding_z)
    long_liq = _latest(rows, "coinalyze", "long_liquidations", symbol)
    short_liq = _latest(rows, "coinalyze", "short_liquidations", symbol)
    imbalance = liquidation_imbalance(long_liq[1] if long_liq else None, short_liq[1] if short_liq else None)
    ratio = _latest(rows, "coinalyze", "long_short_ratio", symbol)
    ratio_z = None
    if ratio is not None:
        past = [log_ratio(value) for _, value in _series(rows, "coinalyze", "long_short_ratio", symbol)[:-1]]
        past = [item for item in past if item is not None]
        ratio_z = signed_from_z(robust_zscore(log_ratio(ratio[1]), past))
    binance_oi = _latest(rows, "binance", "open_interest_value", symbol)
    coinalyze_oi = _latest(rows, "coinalyze", "open_interest_usd", symbol)
    disagreement = None
    if binance_oi and coinalyze_oi:
        disagreement = relative_disagreement(binance_oi[1], coinalyze_oi[1])
    price = _series(rows, "coinalyze", "close", symbol)
    state, confidence = _position_state(_log_shift(price, 300), changes["oi_change_5m"])
    fresh = freshness(age_seconds(now, oi[-1][0] if oi else None), 900) if oi else 0.0
    available = any(item is not None for item in (*changes.values(), crowding, imbalance))
    return {
        "oi_usd": latest,
        "oi_change_5m": _signed_change(changes["oi_change_5m"], oi),
        "oi_change_15m": _signed_change(changes["oi_change_15m"], oi),
        "oi_percentile": rank,
        "funding_raw": funding_latest,
        "funding_bps": funding_bps(funding_latest),
        "funding_crowding": crowding,
        "long_short_crowding": ratio_z,
        "liquidation_imbalance": imbalance,
        "liquidation_intensity": None,
        "position_building_state": state,
        "position_building_confidence": confidence,
        "provider_disagreement": disagreement,
        "quality": feature_quality(available=available, fresh=fresh, disagreement=disagreement),
        "source": "coinalyze" if any(row.provider == "coinalyze" and row.symbol == symbol for row in rows) else "binance",
        "source_timestamp": oi[-1][0].isoformat() if oi else None,
    }


def _sentiment(rows: list[RawExternalObservation], now: datetime) -> dict:
    series = sorted(
        ((row.observed_at, row.value, row.metadata.get("classification")) for row in rows if row.provider == "alternative_me" and row.metric == "fear_greed" and row.value is not None),
        key=lambda item: item[0],
    )
    if not series:
        return {"fear_greed": None, "quality": 0.0, "scope": "BTC_MARKET_SENTIMENT"}
    latest_at, raw, classification = series[-1]
    values = [value for _, value, _ in series]
    rank_30, _ = percentile_signed(values[-30:-1], raw) if len(values) > 2 else (None, None)
    rank_90, _ = percentile_signed(values[:-1], raw) if len(values) > 2 else (None, None)
    fresh = freshness(age_seconds(now, latest_at), _SLOW)
    return {
        "fear_greed": {
            "raw": raw,
            "signed": fear_greed_signed(raw),
            "change_1d": _day_change(series, 1),
            "change_3d": _day_change(series, 3),
            "change_7d": _day_change(series, 7),
            "percentile_30d": rank_30,
            "percentile_90d": rank_90,
            "extreme_fear": classification == "Extreme Fear",
            "extreme_greed": classification == "Extreme Greed",
            "freshness": fresh,
            "scope": "BTC_MARKET_SENTIMENT",
            "source_timestamp": latest_at.isoformat(),
        },
        "quality": feature_quality(available=True, fresh=fresh if fresh is not None else 0.0),
        "scope": "BTC_MARKET_SENTIMENT",
    }


def _dominance(rows, now: datetime, symbol: str) -> dict:
    series = _series(rows, "alternative_me", "btc_dominance", None)
    latest = series[-1][1] if series else None
    change = _shift(series, 3600)
    z = robust_zscore(latest, [value for _, value in series[:-1]]) if latest is not None else None
    return {
        "btc_dominance_raw": latest,
        "btc_dominance_change": change,
        "btc_dominance_zscore": z,
        "alt_rotation_pressure": None,
        "symbol": symbol,
        "source_timestamp": series[-1][0].isoformat() if series else None,
    }


def _relative(symbol: str, rows, now: datetime) -> dict:
    closes = {
        name: _series(rows, "coinalyze", "close", name)
        for name in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    }
    return {
        "eth_vs_btc": _spread(closes["ETHUSDT"], closes["BTCUSDT"]) if symbol != "BTCUSDT" else None,
        "sol_vs_btc": _spread(closes["SOLUSDT"], closes["BTCUSDT"]) if symbol != "BTCUSDT" else None,
        "sol_vs_eth": _spread(closes["SOLUSDT"], closes["ETHUSDT"]) if symbol == "SOLUSDT" else None,
        "btc_relative_strength": _spread(closes["BTCUSDT"], closes["ETHUSDT"]) if symbol == "BTCUSDT" else None,
    }


def _position_state(price_change: float | None, oi_change: float | None) -> tuple[str, float]:
    if price_change is None or oi_change is None or price_change == 0 or oi_change == 0:
        return "UNCERTAIN", 0.0
    if price_change > 0 and oi_change > 0:
        return "LONG_BUILDUP", 0.5
    if price_change > 0 and oi_change < 0:
        return "SHORT_COVERING", 0.5
    if price_change < 0 and oi_change > 0:
        return "SHORT_BUILDUP", 0.5
    return "LONG_UNWINDING", 0.5


def _series(rows, provider: str, metric: str, symbol: str | None) -> list[tuple[datetime, float]]:
    found = [
        (ensure_utc(row.observed_at), row.value)
        for row in rows
        if row.provider == provider and row.metric == metric and row.symbol == symbol and isinstance(row.value, (int, float)) and math.isfinite(row.value)
    ]
    found.sort(key=lambda item: item[0])
    return found


def _latest(rows, provider, metric, symbol):
    series = _series(rows, provider, metric, symbol)
    return series[-1] if series else None


def _log_shift(series: list[tuple[datetime, float]], seconds: int) -> float | None:
    if len(series) < 2:
        return None
    end_at, end = series[-1]
    prior = asof_backward(series[:-1], end_at - timedelta(seconds=seconds), lambda item: item[0])
    if prior is None:
        return None
    return oi_log_change(end, prior[1])


def _shift(series, seconds: int) -> float | None:
    if len(series) < 2:
        return None
    end_at, end = series[-1]
    prior = asof_backward(series[:-1], end_at - timedelta(seconds=seconds), lambda item: item[0])
    if prior is None:
        return None
    return end - prior[1]


def _signed_change(change: float | None, series) -> float | None:
    if change is None:
        return None
    past = []
    for index in range(1, len(series)):
        step = oi_log_change(series[index][1], series[index - 1][1])
        if step is not None:
            past.append(step)
    if len(past) < 3:
        return None
    return signed_from_z(robust_zscore(change, past[:-1] or past))


def _day_change(series, days: int) -> float | None:
    end_at, raw, _ = series[-1]
    prior = asof_backward(series[:-1], end_at - timedelta(days=days), lambda item: item[0])
    if prior is None:
        return None
    return raw - prior[1]


def _spread(left, right) -> float | None:
    if len(left) < 2 or len(right) < 2:
        return None
    move_left = oi_log_change(left[-1][1], left[-2][1])
    move_right = oi_log_change(right[-1][1], right[-2][1])
    if move_left is None or move_right is None:
        return None
    diff = move_left - move_right
    return math.tanh(diff / 0.01)


def _consistency(*values: float | None) -> float | None:
    present = [value for value in values if value is not None and value != 0]
    if len(present) < 2:
        return None
    sign = 1 if present[0] > 0 else -1
    return sum(1 for value in present if (value > 0) == (sign > 0)) / len(present)


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)
