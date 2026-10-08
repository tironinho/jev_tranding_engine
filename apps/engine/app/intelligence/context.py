from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.domain.mathutil import ensure_utc
from app.intelligence.asof import asof_backward
from app.intelligence.freshness import age_seconds, freshness
from app.intelligence.normalize import (
    direction_of,
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

_SLOW = 24 * 3600
_STALE_ONCHAIN = 14 * 24 * 3600
# Fixed priors, not a fitted edge. Positive support favors a long. Activity does not vote.
_ONCHAIN_WEIGHTS = {
    "exchange_flow": 0.35,
    "exchange_supply": 0.25,
    "valuation": 0.20,
    "stablecoin_liquidity": 0.20,
}
# One or two components are not enough to stamp a side. Below this the vote is withheld.
_ONCHAIN_MIN_EVIDENCE = 0.5


def build_context(
    symbol: str,
    features: dict,
    rows: list[RawExternalObservation],
    now: datetime,
    marks: dict | None = None,
) -> dict:
    """Normalized jev_feature_set_v1. Missing stays null."""
    visible = [row for row in rows if ensure_utc(row.observed_at) <= ensure_utc(now)]
    remembered = marks or {}
    micro = _micro(features)
    _apply_flow(micro, symbol, visible)
    derivatives = _derivatives(symbol, visible, now, remembered)
    sentiment = _sentiment(visible, now)
    global_market = _dominance(visible, now, symbol)
    onchain = _onchain(symbol, visible, now)
    quality_rows = [micro["quality"], derivatives["quality"], sentiment["quality"]]
    if onchain["available"]:
        quality_rows.append(onchain["quality"])
    present = [item for item in quality_rows if item is not None]
    overall = sum(present) / len(present) if present else 0.0
    missing = [
        name
        for name, value in {
            "derivatives.oi_change_5m": derivatives.get("oi_change_5m"),
            "derivatives.funding_crowding": derivatives.get("funding_crowding"),
            "derivatives.liquidation_imbalance": derivatives.get("liquidation_imbalance"),
            "sentiment.fear_greed": sentiment.get("fear_greed"),
            "onchain.exchange_flow_pressure": onchain.get("exchange_flow_pressure"),
            "onchain.support": onchain.get("support_long"),
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
        "relative_strength": _relative(symbol, visible, remembered),
        "onchain": onchain,
        "global": global_market,
        "data_quality": {
            "overall": overall,
            "categories": {
                "microstructure": micro["quality"],
                "derivatives": derivatives["quality"],
                "sentiment": sentiment["quality"],
                "onchain": onchain["quality"],
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
    taker_5 = _finite(features.get("taker_flow_5m"))
    spread = _finite(features.get("spread_bps"))
    values = [item for item in (book_5, book_20, taker, taker_5, spread) if item is not None]
    return {
        "taker_imbalance_1m": taker,
        "taker_imbalance_5m": taker_5,
        "book_imbalance_5": book_5,
        "book_imbalance_20": book_20,
        "book_imbalance_consistency": _consistency(book_5, _finite(features.get("imbalance_10")), book_20),
        "cvd_direction": taker_5,
        "spread_bps": spread,
        "spread_stress": None if spread is None else max(0.0, min(1.0, spread / 8.0)),
        "quality": feature_quality(available=bool(values), fresh=1.0 if values else 0.0),
        "source": "binance_book",
    }


def _apply_flow(micro: dict, symbol: str, rows: list[RawExternalObservation]) -> None:
    """5-minute taker and cumulative flow from Coinalyze buy/sell volume. Missing stays null."""
    buys = {stamp: value for stamp, value in _series(rows, "coinalyze", "buy_volume", symbol)}
    sells = {stamp: value for stamp, value in _series(rows, "coinalyze", "sell_volume", symbol)}
    common = sorted(set(buys) & set(sells))
    if not common:
        return
    micro["taker_imbalance_5m"] = taker_imbalance(buys[common[-1]], sells[common[-1]])
    if len(common) >= 2:
        micro["cvd_direction"] = taker_imbalance(sum(buys[stamp] for stamp in common), sum(sells[stamp] for stamp in common))
    present = [
        micro[key]
        for key in ("taker_imbalance_1m", "taker_imbalance_5m", "book_imbalance_5", "book_imbalance_20", "cvd_direction")
        if micro.get(key) is not None
    ]
    micro["quality"] = feature_quality(available=bool(present), fresh=1.0 if present else 0.0)
    if micro["taker_imbalance_1m"] is None and micro["book_imbalance_5"] is None and micro["book_imbalance_20"] is None:
        micro["source"] = "coinalyze"


def _derivatives(symbol: str, rows: list[RawExternalObservation], now: datetime, marks: dict) -> dict:
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
    funding = _longest(
        _series(rows, "coinalyze", "funding_rate", symbol),
        _series(rows, "binance", "funding_rate", symbol),
        _series(rows, "coinalyze", "funding_current", symbol),
    )
    funding_latest = funding[-1][1] if funding else None
    funding_z = robust_zscore(funding_latest, [value for _, value in funding[:-1]]) if funding_latest is not None else None
    crowding = signed_from_z(funding_z)
    predicted = _series(rows, "coinalyze", "predicted_funding", symbol)
    predicted_latest = predicted[-1][1] if predicted else None
    predicted_z = robust_zscore(predicted_latest, [value for _, value in predicted[:-1]]) if predicted_latest is not None else None
    long_liq = _latest(rows, "coinalyze", "long_liquidations", symbol)
    short_liq = _latest(rows, "coinalyze", "short_liquidations", symbol)
    imbalance = liquidation_imbalance(long_liq[1] if long_liq else None, short_liq[1] if short_liq else None)
    intensity = _liquidation_intensity(rows, symbol)
    ratio = _latest(rows, "coinalyze", "long_short_ratio", symbol)
    ratio_z = None
    if ratio is not None:
        past = [log_ratio(value) for _, value in _series(rows, "coinalyze", "long_short_ratio", symbol)[:-1]]
        past = [item for item in past if item is not None]
        ratio_z = signed_from_z(robust_zscore(log_ratio(ratio[1]), past))
    borrow = _latest(rows, "binance", "borrow_hourly_interest", symbol)
    quote_borrow = _latest(rows, "binance", "quote_borrow_hourly_interest", symbol)
    price_index = _latest(rows, "binance", "margin_price_index", symbol)
    binance_oi = _latest(rows, "binance", "open_interest_value", symbol)
    coinalyze_oi = _latest(rows, "coinalyze", "open_interest_usd", symbol)
    disagreement = None
    if binance_oi and coinalyze_oi:
        disagreement = relative_disagreement(binance_oi[1], coinalyze_oi[1])
    price = _series(rows, "coinalyze", "close", symbol)
    if len(price) < 2:
        price = list(marks.get(symbol) or [])
    state, confidence = _position_state(_log_shift(price, 300), changes["oi_change_5m"])
    fresh = freshness(age_seconds(now, oi[-1][0] if oi else None), 900) if oi else 0.0
    available = any(item is not None for item in (*changes.values(), crowding, imbalance, borrow, quote_borrow, price_index))
    return {
        "oi_usd": latest,
        "oi_change_5m": _signed_change(changes["oi_change_5m"], oi),
        "oi_change_15m": _signed_change(changes["oi_change_15m"], oi),
        "oi_change_1h": _signed_change(changes["oi_change_1h"], oi),
        "oi_percentile": rank,
        "funding_raw": funding_latest,
        "borrow_hourly_interest": borrow[1] if borrow else None,
        "quote_borrow_hourly_interest": quote_borrow[1] if quote_borrow else None,
        "margin_price_index": price_index[1] if price_index else None,
        "funding_bps": funding_bps(funding_latest),
        "funding_crowding": crowding,
        "predicted_funding_bps": funding_bps(predicted_latest),
        "predicted_funding_crowding": signed_from_z(predicted_z),
        "long_short_crowding": ratio_z,
        "liquidation_imbalance": imbalance,
        "liquidation_intensity": intensity,
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


def _onchain(symbol: str, rows: list[RawExternalObservation], now: datetime) -> dict:
    btc_flow = _btc_flow(rows, now)
    cq_stables = _recent(_series(rows, "cryptoquant", "stablecoin_reserve", None), now) or _recent(_series(rows, "cryptoquant", "stablecoin_supply", None), now)
    cm_stables = _recent(_series(rows, "coinmetrics", "stablecoin_supply", None), now)
    stables = cq_stables or cm_stables
    pressure = _signed_latest(btc_flow)
    liquidity = _signed_latest(stables)
    own_flow = btc_flow if symbol == "BTCUSDT" else _recent(_series(rows, "coinmetrics", "exchange_netflow", symbol), now)
    supply = _recent(_series(rows, "coinmetrics", "exchange_supply", symbol), now)
    mvrv = _recent(_series(rows, "coinmetrics", "mvrv", symbol), now)
    activity = _recent(_series(rows, "coinmetrics", "active_addresses", symbol), now) or _recent(_series(rows, "coinmetrics", "tx_count", symbol), now)
    fees = _recent(_series(rows, "coinmetrics", "fee_native", symbol), now)
    flow_score = _flip(_signed_latest(own_flow))
    supply_score = _flip(_signed_latest(supply))
    stretch = _signed_latest(mvrv)
    activity_score = _signed_latest(activity)
    stress = _signed_latest(fees)
    parts = {
        "exchange_flow": flow_score,
        "exchange_supply": supply_score,
        "valuation": _flip(stretch),
        "stablecoin_liquidity": liquidity,
    }
    present = {name: value for name, value in parts.items() if value is not None}
    weight_total = sum(_ONCHAIN_WEIGHTS.values())
    evidence = sum(_ONCHAIN_WEIGHTS[name] for name in present) / weight_total if weight_total else 0.0
    support_long = sum(_ONCHAIN_WEIGHTS[name] * value for name, value in present.items()) / sum(_ONCHAIN_WEIGHTS[name] for name in present) if present else None
    latest = btc_flow[-1][0] if btc_flow else (stables[-1][0] if stables else None)
    if own_flow:
        latest = own_flow[-1][0]
    fresh = freshness(age_seconds(now, latest), _SLOW) if latest else 0.0
    available = support_long is not None or activity_score is not None or stress is not None or _latest_value(btc_flow) is not None
    names = []
    if _series(rows, "cryptoquant", "exchange_netflow", None) or cq_stables:
        names.append("cryptoquant")
    if _has_coinmetrics(rows, symbol) or (cm_stables and not cq_stables):
        names.append("coinmetrics")
    return {
        "exchange_netflow_btc": _latest_value(btc_flow),
        "exchange_flow_pressure": pressure,
        "exchange_flow": flow_score,
        "exchange_supply": supply_score,
        "valuation_stretch": stretch,
        "activity": activity_score,
        "network_stress": stress,
        "stablecoin_reserve": _latest_value(stables),
        "stablecoin_liquidity": liquidity,
        "support_long": support_long,
        "evidence": evidence if present else 0.0,
        "weights": dict(_ONCHAIN_WEIGHTS),
        "available": available,
        "quality": feature_quality(available=available, fresh=fresh if fresh is not None else 0.0),
        "source": "+".join(names) if names else None,
        "source_timestamp": latest.isoformat() if latest else None,
    }


def jev_view(payload: dict | None, baseline_action: str | None) -> dict | None:
    """Unitless state for the veto. Dollar stocks stay off the request."""
    if not isinstance(payload, dict):
        return None
    return {
        "microstructure": payload.get("microstructure"),
        "derivatives": _without(payload.get("derivatives"), {"oi_usd", "margin_price_index"}),
        "sentiment": _sentiment_view(payload.get("sentiment")),
        "relative_strength": payload.get("relative_strength"),
        "global": _without(payload.get("global"), {"btc_dominance_raw", "symbol"}),
        "onchain": _onchain_view(payload.get("onchain"), baseline_action),
        "data_quality": payload.get("data_quality"),
    }


def _onchain_view(raw, baseline_action: str | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    view = {
        key: value
        for key, value in raw.items()
        if key not in {"exchange_netflow_btc", "exchange_flow_pressure", "stablecoin_reserve", "support_long", "source", "source_timestamp"}
    }
    support_long = raw.get("support_long")
    evidence = raw.get("evidence")
    side = (baseline_action or "").upper()
    thin = not isinstance(evidence, (int, float)) or float(evidence) < _ONCHAIN_MIN_EVIDENCE
    if not isinstance(support_long, (int, float)) or thin:
        view["support"] = None
        view["class"] = "UNKNOWN"
        return view
    support = -float(support_long) if side == "SHORT" else float(support_long)
    view["support"] = support
    view["class"] = _class_of(support)
    return view


def _class_of(support: float) -> str:
    direction = direction_of(support)
    if direction in {"STRONGLY_POSITIVE", "POSITIVE"}:
        return "SUPPORTIVE"
    if direction in {"STRONGLY_NEGATIVE", "NEGATIVE"}:
        return "HOSTILE"
    return "NEUTRAL"


def _sentiment_view(raw):
    if not isinstance(raw, dict):
        return raw
    fear = raw.get("fear_greed")
    if not isinstance(fear, dict):
        return raw
    return {
        "fear_greed_signed": fear.get("signed"),
        "extreme_fear": fear.get("extreme_fear"),
        "extreme_greed": fear.get("extreme_greed"),
        "freshness": fear.get("freshness"),
        "quality": raw.get("quality"),
    }


def _without(raw, dropped: set[str]):
    if not isinstance(raw, dict):
        return raw
    return {key: value for key, value in raw.items() if key not in dropped}


def _btc_flow(rows, now: datetime) -> list[tuple[datetime, float]]:
    return (
        _recent(_series(rows, "cryptoquant", "exchange_netflow", None), now)
        or _recent(_series(rows, "coinmetrics", "exchange_netflow", "BTCUSDT"), now)
        or _recent(_series(rows, "coinmetrics", "exchange_netflow", None), now)
    )


def _has_coinmetrics(rows, symbol: str) -> bool:
    metrics = {"exchange_netflow", "exchange_supply", "mvrv", "active_addresses", "tx_count", "fee_native"}
    return any(row.provider == "coinmetrics" and row.metric in metrics and row.symbol in {symbol, "BTCUSDT", None} for row in rows)


def _recent(series: list[tuple[datetime, float]], now: datetime) -> list[tuple[datetime, float]]:
    if not series:
        return []
    age = age_seconds(now, series[-1][0])
    if age is None or age < 0 or age > _STALE_ONCHAIN:
        return []
    return series


def _flip(value: float | None) -> float | None:
    if value is None:
        return None
    return -value


def _latest_value(series: list[tuple[datetime, float]]) -> float | None:
    return series[-1][1] if series else None


def _signed_latest(series: list[tuple[datetime, float]]) -> float | None:
    if len(series) < 2:
        return None
    latest = series[-1][1]
    history = [value for _, value in series[:-1]]
    return signed_from_z(robust_zscore(latest, history))


def _dominance(rows, now: datetime, symbol: str) -> dict:
    series = _series(rows, "alternative_me", "btc_dominance", None)
    latest = series[-1][1] if series else None
    change = _shift(series, 3600)
    if change is None and len(series) >= 2:
        change = series[-1][1] - series[0][1]
    z = robust_zscore(latest, [value for _, value in series[:-1]]) if latest is not None else None
    scale = 0.01 if latest is not None and abs(latest) <= 2 else 1.0
    rotation = None if change is None else max(-1.0, min(1.0, math.tanh(-change / scale)))
    return {
        "btc_dominance_raw": latest,
        "btc_dominance_change": change,
        "btc_dominance_zscore": z,
        "alt_rotation_pressure": rotation,
        "symbol": symbol,
        "source_timestamp": series[-1][0].isoformat() if series else None,
    }


def _relative(symbol: str, rows, marks: dict) -> dict:
    closes = {}
    for name in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"):
        series = _series(rows, "coinalyze", "close", name)
        closes[name] = series if len(series) >= 2 else list(marks.get(name) or series)
    return {
        "eth_vs_btc": _spread(closes["ETHUSDT"], closes["BTCUSDT"]) if symbol != "BTCUSDT" else None,
        "sol_vs_btc": _spread(closes["SOLUSDT"], closes["BTCUSDT"]) if symbol != "BTCUSDT" else None,
        "sol_vs_eth": _spread(closes["SOLUSDT"], closes["ETHUSDT"]) if symbol == "SOLUSDT" else None,
        "bnb_vs_btc": _spread(closes["BNBUSDT"], closes["BTCUSDT"]) if symbol == "BNBUSDT" else None,
        "xrp_vs_btc": _spread(closes["XRPUSDT"], closes["BTCUSDT"]) if symbol == "XRPUSDT" else None,
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


def _liquidation_intensity(rows, symbol: str) -> float | None:
    """How large the latest liquidation bar is versus the recent window. Not a side."""
    longs = {stamp: value for stamp, value in _series(rows, "coinalyze", "long_liquidations", symbol)}
    shorts = {stamp: value for stamp, value in _series(rows, "coinalyze", "short_liquidations", symbol)}
    stamps = sorted(set(longs) & set(shorts))
    if len(stamps) < 4:
        return None
    totals = [longs[stamp] + shorts[stamp] for stamp in stamps]
    rank, _signed = percentile_signed(totals[:-1], totals[-1])
    return rank


def _longest(*series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    present = [item for item in series if item]
    if not present:
        return []
    return max(present, key=len)


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
