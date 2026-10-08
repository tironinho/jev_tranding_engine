from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.market.parse import parse_rest_klines
from app.market.state import Candle


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def kline_path(market_type: str) -> str:
    """Spot and margin read the spot book. Futures is the only other path."""
    if market_type == "futures":
        return "/fapi/v1/klines"
    return "/api/v3/klines"


async def download_klines(
    client: Any,
    *,
    base_url: str,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    market_type: str = "spot",
    limit: int = 1000,
) -> list[Candle]:
    """Page public klines. The host is the caller's. Taker-buy volume stays on the candle."""
    path = kline_path(market_type)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    candles: list[Candle] = []
    seen: set[datetime] = set()
    while start_ms < end_ms:
        response = await client.get(
            f"{base_url.rstrip('/')}{path}",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            break
        page = parse_rest_klines(symbol, interval, rows)
        end_utc = _utc(end)
        for candle in page:
            if candle.close_time > end_utc:
                continue
            if candle.open_time in seen:
                continue
            seen.add(candle.open_time)
            candles.append(candle)
        last_open = rows[-1][0]
        if not isinstance(last_open, (int, float)):
            break
        nxt = int(last_open) + 1
        if nxt <= start_ms or len(rows) < limit:
            break
        start_ms = nxt
    candles.sort(key=lambda candle: candle.open_time)
    return candles
