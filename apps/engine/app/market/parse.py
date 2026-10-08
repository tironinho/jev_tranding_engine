from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market.state import AggTrade, Candle, Level, OrderBook


def _dt_ms(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def parse_rest_klines(symbol: str, timeframe: str, rows: list) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        open_ = _f(row[1])
        high = _f(row[2])
        low = _f(row[3])
        close = _f(row[4])
        volume = _f(row[5])
        if None in (open_, high, low, close, volume):
            continue
        if min(open_, high, low, close) <= 0 or high < low:
            continue
        taker_buy = _f(row[9]) if len(row) > 9 else None
        candles.append(
            Candle(
                open_time=_dt_ms(row[0]),
                close_time=_dt_ms(row[6]),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                taker_buy_volume=taker_buy,
                closed=True,
                timeframe=timeframe,
            )
        )
    return candles


def parse_combined(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    if "data" in message and isinstance(message["data"], dict):
        return message["data"]
    return message


def stream_symbol(payload: dict) -> str | None:
    data = parse_combined(payload) or {}
    symbol = data.get("s")
    if isinstance(symbol, str) and symbol:
        return symbol.upper()
    stream = payload.get("stream") if isinstance(payload, dict) else None
    if isinstance(stream, str) and "@" in stream:
        return stream.split("@", 1)[0].upper()
    return None


def apply_market_message(state, payload: dict, received_at: datetime) -> str | None:
    """Mutate state from one Binance public event. Returns 'kline_close_1m' when a timing bar closes."""
    data = parse_combined(payload)
    if not data:
        return None
    event = data.get("e")
    symbol = data.get("s")
    if symbol and symbol != state.symbol:
        return None
    if event is None and "b" in data and "a" in data and "bids" not in data:
        event = "bookTicker"
    if event is None and "bids" in data and "asks" in data:
        event = "depthUpdate"
    event_time = data.get("E") or data.get("T")
    if isinstance(event_time, (int, float)):
        state.latency_ms = max(0.0, (received_at - _dt_ms(event_time)).total_seconds() * 1000)
        if _dt_ms(event_time) > received_at and ( _dt_ms(event_time) - received_at).total_seconds() > 60:
            return None
    if event == "kline":
        return _apply_kline(state, data)
    if event == "aggTrade":
        price = _f(data.get("p"))
        qty = _f(data.get("q"))
        trade_time = data.get("T")
        if price is None or qty is None or price <= 0 or not isinstance(trade_time, (int, float)):
            return None
        side = "sell" if data.get("m") else "buy"
        state.add_trade(AggTrade(timestamp=_dt_ms(trade_time), price=price, quantity=qty, aggressive_side=side))
        return "trade"
    if event == "bookTicker":
        bid = _f(data.get("b"))
        ask = _f(data.get("a"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            return None
        state.best_bid = bid
        state.best_ask = ask
        state.bid_qty = _f(data.get("B"))
        state.ask_qty = _f(data.get("A"))
        stamp = _dt_ms(event_time) if isinstance(event_time, (int, float)) else received_at
        state.last_book_at = stamp
        state.last_price = (bid + ask) / 2
        state.last_event_at = stamp
        return "book"
    if event == "depthUpdate":
        bids = _levels(data.get("b") or data.get("bids"))
        asks = _levels(data.get("a") or data.get("asks"))
        if not bids or not asks or bids[0].price > asks[0].price:
            return None
        stamp = _dt_ms(data.get("T") or data.get("E") or received_at.timestamp() * 1000)
        state.book = OrderBook(bids=bids, asks=asks, timestamp=stamp)
        state.best_bid = bids[0].price
        state.best_ask = asks[0].price
        state.last_book_at = stamp
        state.last_price = (bids[0].price + asks[0].price) / 2
        state.last_event_at = stamp
        return "depth"
    if event == "markPriceUpdate":
        mark = _f(data.get("p"))
        index = _f(data.get("i"))
        funding = _f(data.get("r"))
        if mark is not None:
            state.mark_price = mark
        if index is not None:
            state.index_price = index
        if funding is not None:
            if state.funding_rate is not None and funding != state.funding_rate:
                state.previous_funding_rate = state.funding_rate
            state.funding_rate = funding
        state.last_event_at = received_at
        return "mark"
    return None


def _apply_kline(state, data: dict) -> str | None:
    candle = data.get("k") or {}
    timeframe = candle.get("i")
    if timeframe not in {"1m", "5m", "15m"}:
        return None
    if not isinstance(candle.get("t"), (int, float)) or not isinstance(candle.get("T"), (int, float)):
        return None
    open_ = _f(candle.get("o"))
    high = _f(candle.get("h"))
    low = _f(candle.get("l"))
    close = _f(candle.get("c"))
    volume = _f(candle.get("v"))
    if None in (open_, high, low, close, volume) or min(open_, high, low, close) <= 0 or high < low:
        return None
    closed = bool(candle.get("x"))
    state.upsert_candle(
        Candle(
            open_time=_dt_ms(candle.get("t")),
            close_time=_dt_ms(candle.get("T")),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume or 0,
            taker_buy_volume=_f(candle.get("V")),
            closed=closed,
            timeframe=timeframe,
        )
    )
    if closed and timeframe == "1m":
        return "kline_close_1m"
    return "kline"


def _levels(rows: Any) -> list[Level]:
    levels: list[Level] = []
    if not isinstance(rows, list):
        return levels
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = _f(row[0])
        qty = _f(row[1])
        if price is None or qty is None or price <= 0 or qty < 0:
            continue
        if qty == 0:
            continue
        levels.append(Level(price=price, quantity=qty))
    return levels


def parse_exchange_filters(payload: dict, symbols: set[str]) -> dict[str, dict[str, float]]:
    found: dict[str, dict[str, float]] = {}
    for item in payload.get("symbols") or []:
        name = item.get("symbol")
        if name not in symbols:
            continue
        rules: dict[str, float] = {}
        for filt in item.get("filters") or []:
            if filt.get("filterType") == "PRICE_FILTER":
                tick = _f(filt.get("tickSize"))
                if tick:
                    rules["tick_size"] = tick
            if filt.get("filterType") == "LOT_SIZE":
                step = _f(filt.get("stepSize"))
                if step:
                    rules["step_size"] = step
        if rules:
            found[name] = rules
    return found
