from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.enums import MarketType
from app.domain.schemas import DataQuality, MarketSnapshot
from app.features.indicators import (
    atr,
    ema,
    ema_slope,
    percentile_rank,
    realized_volatility,
    roc,
    rsi,
    vwap_from_candles,
    zscore,
)
from app.market.state import Candle, OrderBook, SymbolMarketState

REQUIRED_FOR_BASELINE = (
    "return_1m",
    "ema_9",
    "ema_20",
    "ema_50",
    "ema_alignment",
    "rsi",
    "atr",
    "atr_normalized",
    "volume_ratio",
    "spread_bps",
)


def closed_candles(candles: list[Candle], as_of: datetime) -> list[Candle]:
    """Candles whose close is already known at as_of. Forming bars are excluded."""
    return [candle for candle in candles if candle.closed and candle.close_time <= as_of]


def quantitative_regime(features: dict) -> str:
    alignment = features.get("ema_alignment")
    vol_pct = features.get("volatility_percentile")
    if features.get("breakout") is True:
        structure = "breakout"
    elif features.get("breakdown") is True:
        structure = "breakdown"
    elif isinstance(alignment, (int, float)) and alignment > 0.5:
        structure = "bull_trend"
    elif isinstance(alignment, (int, float)) and alignment < -0.5:
        structure = "bear_trend"
    else:
        structure = "range"
    if isinstance(vol_pct, (int, float)) and vol_pct >= 0.8:
        vol = "high_volatility"
    elif isinstance(vol_pct, (int, float)) and vol_pct <= 0.2:
        vol = "low_volatility"
    else:
        vol = "normal_volatility"
    return f"{structure}|{vol}"


_LIVE_BOOK = timedelta(seconds=15)


def _at_decision(stamp: datetime | None, as_of: datetime) -> bool:
    """The book at the decision counts. A book from a later replay does not."""
    if stamp is None:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    moment = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
    if stamp <= moment:
        return True
    return stamp - moment <= _LIVE_BOOK


def _taker_ratio(candles: list[Candle]) -> float | None:
    if not candles or any(candle.taker_buy_volume is None for candle in candles):
        return None
    volume = sum(candle.volume for candle in candles)
    if volume <= 0:
        return None
    buy = sum(candle.taker_buy_volume or 0 for candle in candles)
    return max(-1.0, min(1.0, (2 * buy / volume) - 1))


def _book_depth(book: OrderBook, depth: int) -> tuple[float, float]:
    bid_qty = sum(level.quantity for level in book.bids[:depth])
    ask_qty = sum(level.quantity for level in book.asks[:depth])
    return bid_qty, ask_qty


def compute_features(state: SymbolMarketState, as_of: datetime, price: float) -> tuple[dict, DataQuality]:
    """Deterministic features using only information timestamped at or before as_of."""
    features: dict = {}
    warnings: list[str] = []
    candles_1m = closed_candles(state.candles.get("1m", []), as_of)
    candles_5m = closed_candles(state.candles.get("5m", []), as_of)
    candles_15m = closed_candles(state.candles.get("15m", []), as_of)
    closes = [candle.close for candle in candles_1m]
    highs = [candle.high for candle in candles_1m]
    lows = [candle.low for candle in candles_1m]
    volumes = [candle.volume for candle in candles_1m]

    def ret(bars: int) -> float | None:
        if len(closes) <= bars or closes[-bars - 1] == 0:
            return None
        return price / closes[-bars - 1] - 1

    features["return_1m"] = ret(1)
    features["return_3m"] = ret(3)
    features["return_5m"] = ret(5)
    features["return_15m"] = ret(15)
    features["return_60m"] = ret(60)
    features["return_since_close"] = (price / closes[-1] - 1) if closes and closes[-1] else None

    lookback = closes[-50:] if closes else []
    if lookback and price:
        window_high = max(highs[-50:])
        window_low = min(lows[-50:])
        features["distance_from_high"] = price / window_high - 1 if window_high else None
        features["distance_from_low"] = price / window_low - 1 if window_low else None
    else:
        features["distance_from_high"] = None
        features["distance_from_low"] = None

    features["ema_9"] = ema(closes, 9)
    features["ema_20"] = ema(closes, 20)
    features["ema_50"] = ema(closes, 50)
    features["ema_200"] = ema(closes, 200)
    features["ema_9_slope"] = ema_slope(closes, 9)
    features["ema_20_slope"] = ema_slope(closes, 20)
    features["ema_50_slope"] = ema_slope(closes, 50)
    features["ema_200_slope"] = ema_slope(closes, 200)
    ema20 = features["ema_20"]
    features["price_vs_ema20"] = (price / ema20 - 1) if ema20 else None
    ordered = [
        value
        for value in (features["ema_9"], ema20, features["ema_50"], features["ema_200"])
        if isinstance(value, (int, float))
    ]
    if len(ordered) >= 2:
        steps = len(ordered) - 1
        bull = all(ordered[i] > ordered[i + 1] for i in range(steps))
        bear = all(ordered[i] < ordered[i + 1] for i in range(steps))
        if bull:
            features["ema_alignment"] = 1.0
        elif bear:
            features["ema_alignment"] = -1.0
        else:
            pairwise = 0
            for left, right in zip(ordered, ordered[1:], strict=False):
                pairwise += 1 if left > right else -1
            features["ema_alignment"] = pairwise / steps
    else:
        features["ema_alignment"] = None

    day = as_of.astimezone(timezone.utc).date()
    typical: list[float] = []
    day_volumes: list[float] = []
    for candle in candles_1m:
        if candle.open_time.astimezone(timezone.utc).date() != day:
            continue
        typical.append((candle.high + candle.low + candle.close) / 3)
        day_volumes.append(candle.volume)
    features["vwap"] = vwap_from_candles(typical, day_volumes)
    vwap = features["vwap"]
    features["distance_to_vwap"] = (price / vwap - 1) if vwap else None
    if len(typical) > 6 and len(day_volumes) > 6:
        previous = vwap_from_candles(typical[:-5], day_volumes[:-5])
        current = features["vwap"]
        features["vwap_slope"] = ((current - previous) / abs(previous)) if current and previous else None
    else:
        features["vwap_slope"] = None

    features["rsi"] = rsi(closes, 14)
    features["roc"] = roc(closes, 10)
    features["momentum_1m"] = features["return_1m"]
    features["momentum_5m"] = features["return_5m"]
    if len(closes) > 3 and closes[-2] and closes[-3]:
        mom_now = closes[-1] / closes[-2] - 1
        mom_prev = closes[-2] / closes[-3] - 1
        features["momentum_acceleration"] = mom_now - mom_prev
    else:
        features["momentum_acceleration"] = None

    features["atr"] = atr(highs, lows, closes, 14)
    atr_value = features["atr"]
    features["atr_normalized"] = (atr_value / price) if atr_value and price else None
    features["realized_volatility"] = realized_volatility(closes, 30)
    vol_history: list[float] = []
    if len(closes) >= 80:
        for end in range(31, len(closes) + 1):
            value = realized_volatility(closes[:end], 30)
            if value is not None:
                vol_history.append(value)
    current_vol = features["realized_volatility"]
    features["volatility_percentile"] = (
        percentile_rank(vol_history[:-1], current_vol) if current_vol is not None and len(vol_history) > 2 else None
    )

    features["current_volume"] = volumes[-1] if volumes else None
    avg_source = volumes[-21:-1] if len(volumes) > 21 else volumes[:-1]
    features["average_volume"] = (sum(avg_source) / len(avg_source)) if avg_source else None
    avg_vol = features["average_volume"]
    cur_vol = features["current_volume"]
    features["volume_ratio"] = (cur_vol / avg_vol) if cur_vol is not None and avg_vol else None
    features["volume_zscore"] = zscore(cur_vol, avg_source) if cur_vol is not None and avg_source else None

    if len(highs) >= 60 and len(lows) >= 60:
        features["range_60m"] = max(highs[-60:]) - min(lows[-60:])
    else:
        features["range_60m"] = None
    span = features["range_60m"]
    features["range_60m_frac"] = (span / price) if isinstance(span, (int, float)) and span > 0 and price > 0 else None
    recent_bars = candles_1m[-5:]
    recent_spans = [bar.high - bar.low for bar in recent_bars if bar.high >= bar.low]
    features["range_1m"] = max(recent_spans) if recent_spans else None

    features["taker_flow_1m"] = _taker_ratio(candles_1m[-1:])
    features["taker_flow_5m"] = _taker_ratio(candles_1m[-5:])

    window_start = as_of - timedelta(seconds=60)
    trades = [trade for trade in state.trades if window_start <= trade.timestamp <= as_of]
    buy_vol = sum(trade.quantity for trade in trades if trade.aggressive_side == "buy")
    sell_vol = sum(trade.quantity for trade in trades if trade.aggressive_side == "sell")
    features["aggressive_buy_volume"] = buy_vol
    features["aggressive_sell_volume"] = sell_vol
    features["delta"] = buy_vol - sell_vol
    total_aggr = buy_vol + sell_vol
    features["buy_sell_ratio"] = (buy_vol / sell_vol) if sell_vol else None
    features["orderflow_delta_ratio"] = ((buy_vol - sell_vol) / total_aggr) if total_aggr else None
    session_trades = [
        trade
        for trade in state.trades
        if trade.timestamp <= as_of and trade.timestamp.astimezone(timezone.utc).date() == day
    ]
    cvd = 0.0
    cvd_points: list[tuple[datetime, float]] = []
    for trade in session_trades:
        cvd += trade.quantity if trade.aggressive_side == "buy" else -trade.quantity
        cvd_points.append((trade.timestamp, cvd))
    features["cvd"] = cvd if session_trades else None
    slope_cut = as_of - timedelta(seconds=30)
    older = [point for point in cvd_points if point[0] <= slope_cut]
    features["cvd_slope"] = (cvd - older[-1][1]) if older else None

    book = state.book if state.book and _at_decision(state.book.timestamp, as_of) else None
    bid = state.best_bid if _at_decision(state.last_book_at, as_of) else None
    ask = state.best_ask if _at_decision(state.last_book_at, as_of) else None
    if book and book.bids and book.asks:
        bid = book.bids[0].price
        ask = book.asks[0].price
        for depth in (5, 10, 20):
            bid_qty, ask_qty = _book_depth(book, depth)
            features[f"bid_liquidity_{depth}"] = bid_qty
            features[f"ask_liquidity_{depth}"] = ask_qty
            denom = bid_qty + ask_qty
            features[f"imbalance_{depth}"] = ((bid_qty - ask_qty) / denom) if denom else None
        bq = book.bids[0].quantity
        aq = book.asks[0].quantity
        features["microprice"] = ((ask * bq + bid * aq) / (bq + aq)) if (bq + aq) else None
        notion_bid = sum(level.price * level.quantity for level in book.bids[:10])
        notion_ask = sum(level.price * level.quantity for level in book.asks[:10])
        qty_bid = sum(level.quantity for level in book.bids[:10])
        qty_ask = sum(level.quantity for level in book.asks[:10])
        if qty_bid and qty_ask:
            features["weighted_mid_price"] = ((notion_bid / qty_bid) + (notion_ask / qty_ask)) / 2
        else:
            features["weighted_mid_price"] = None
        features["depth_imbalance"] = features.get("imbalance_10")
    else:
        for depth in (5, 10, 20):
            features[f"bid_liquidity_{depth}"] = None
            features[f"ask_liquidity_{depth}"] = None
            features[f"imbalance_{depth}"] = None
        features["microprice"] = None
        features["weighted_mid_price"] = None
        features["depth_imbalance"] = None

    if bid and ask and ask >= bid and price:
        features["spread_abs"] = ask - bid
        mid = (ask + bid) / 2
        features["spread_bps"] = ((ask - bid) / mid * 10_000) if mid else None
    else:
        features["spread_abs"] = None
        features["spread_bps"] = None

    if len(highs) >= 21:
        prior_high = max(highs[-21:-1])
        prior_low = min(lows[-21:-1])
        features["recent_high"] = prior_high
        features["recent_low"] = prior_low
        features["support"] = prior_low
        features["resistance"] = prior_high
        features["recent_swing_low"] = min(lows[-8:-1]) if len(lows) >= 8 else prior_low
        features["recent_swing_high"] = max(highs[-8:-1]) if len(highs) >= 8 else prior_high
        features["distance_to_support"] = (price / prior_low - 1) if prior_low else None
        features["distance_to_resistance"] = (price / prior_high - 1) if prior_high else None
        features["breakout"] = bool(closes[-1] > prior_high)
        features["breakdown"] = bool(closes[-1] < prior_low)
        span = prior_high - prior_low
        features["range_position"] = ((price - prior_low) / span) if span else None
    else:
        for name in (
            "recent_high",
            "recent_low",
            "support",
            "resistance",
            "recent_swing_low",
            "recent_swing_high",
            "distance_to_support",
            "distance_to_resistance",
            "range_position",
        ):
            features[name] = None
        features["breakout"] = None
        features["breakdown"] = None

    if state.market_type == MarketType.FUTURES.value:
        features["funding_rate"] = state.funding_rate
        prev_funding = state.previous_funding_rate
        features["funding_change"] = (
            (state.funding_rate - prev_funding) if state.funding_rate is not None and prev_funding is not None else None
        )
        features["open_interest"] = state.open_interest
        prev_oi = state.previous_open_interest
        features["open_interest_change"] = (
            (state.open_interest / prev_oi - 1) if state.open_interest and prev_oi else None
        )
        if state.mark_price and state.index_price:
            features["mark_index_divergence"] = state.mark_price / state.index_price - 1
        else:
            features["mark_index_divergence"] = None
    else:
        for name in (
            "funding_rate",
            "funding_change",
            "open_interest",
            "open_interest_change",
            "mark_index_divergence",
        ):
            features[name] = None

    context_closes_5 = [candle.close for candle in candles_5m]
    context_closes_15 = [candle.close for candle in candles_15m]
    features["ema_20_5m"] = ema(context_closes_5, 20)
    features["ema_20_15m"] = ema(context_closes_15, 20)
    features["context_15m_slope"] = ema_slope(context_closes_15, 20)
    highs_15 = [candle.high for candle in candles_15m]
    lows_15 = [candle.low for candle in candles_15m]
    if len(highs_15) >= 21:
        # Prior 20 closed 15m bars, excluding the latest closed bar, so a break of that bar is not its own target.
        features["resistance_15m"] = max(highs_15[-21:-1])
        features["support_15m"] = min(lows_15[-21:-1])
    else:
        features["resistance_15m"] = None
        features["support_15m"] = None
    features["setup_5m_return"] = (
        (context_closes_5[-1] / context_closes_5[-2] - 1) if len(context_closes_5) >= 2 and context_closes_5[-2] else None
    )

    missing = [name for name in REQUIRED_FOR_BASELINE if features.get(name) is None]
    if not candles_1m:
        warnings.append("no_closed_candles")
    quality = DataQuality(
        stale=False,
        missing_features=missing,
        warnings=warnings,
        book_available=book is not None or (bid is not None and ask is not None),
        trades_available=bool(trades),
        candles_available=len(candles_1m) >= 50,
        derivatives_available=state.market_type == MarketType.FUTURES.value and state.funding_rate is not None,
    )
    return features, quality


def build_snapshot(
    state: SymbolMarketState,
    *,
    as_of: datetime,
    trigger: str,
    stale_after_ms: int,
    received_latency_ms: float | None = None,
) -> MarketSnapshot | None:
    price = state.last_price
    if price is None or price <= 0:
        return None
    features, quality = compute_features(state, as_of, price)
    book_at = state.last_book_at
    if book_at is None:
        quality.warnings.append("missing_book")
    fresh_at = [moment for moment in (book_at, state.last_trade_at, state.last_event_at) if moment is not None]
    staleness_ms = None
    stale = not fresh_at
    if fresh_at:
        newest = max(fresh_at)
        staleness_ms = int((as_of - newest).total_seconds() * 1000)
        stale = staleness_ms > stale_after_ms
    quality = quality.model_copy(update={"stale": stale, "staleness_ms": staleness_ms})
    bid = state.best_bid
    ask = state.best_ask
    spread = (ask - bid) if bid is not None and ask is not None else features.get("spread_abs")
    reference = f"{state.market_type}:{state.symbol}:{trigger}:{as_of.isoformat()}"
    return MarketSnapshot(
        timestamp=as_of,
        symbol=state.symbol,
        market_type=MarketType(state.market_type),
        raw_market_data_reference=reference,
        price=price,
        best_bid=bid,
        best_ask=ask,
        spread=spread,
        spread_bps=features.get("spread_bps"),
        features=features,
        data_quality=quality,
        latency_ms=received_latency_ms if received_latency_ms is not None else state.latency_ms,
        quantitative_regime=quantitative_regime(features),
        trigger=trigger,
    )
