from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.intelligence.http import fetch_json
from app.intelligence.schemas import ProviderHealth, RawExternalObservation
from app.resilience.guards import CircuitBreaker, TokenBucket

_ASSETS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}


class BinanceIntelligenceProvider:
    """Futures derivatives only. The spot book already feeds price, depth and trades."""

    name = "binance"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.breaker = CircuitBreaker("binance", failure_threshold=5, recovery_seconds=60)
        self.bucket = TokenBucket(rate=2, capacity=8)
        self._status = "ONLINE"
        self._error: str | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        rows: list[RawExternalObservation] = []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return rows
        wanted = [symbol for symbol in symbols if symbol in _ASSETS]
        headers = {}
        if self.settings.binance_api_key:
            headers["X-MBX-APIKEY"] = self.settings.binance_api_key
        base = self.settings.binance_futures_rest_url.rstrip("/")
        blocked = False
        for symbol in wanted:
            public = await self._public(base, symbol, now, headers)
            rows.extend(public)
            if self.settings.binance_api_key:
                extra, geo = await self._keyed(base, symbol, now, headers)
                rows.extend(extra)
                blocked = blocked or geo
        self._status = "DEGRADED" if blocked and not rows else "ONLINE" if rows or not wanted else "DEGRADED"
        if blocked and not rows:
            self._error = "HTTP 451"
        else:
            self._error = None
        return rows

    async def _public(self, base: str, symbol: str, now: datetime, headers: dict) -> list[RawExternalObservation]:
        rows: list[RawExternalObservation] = []
        oi = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/fapi/v1/openInterest", {"symbol": symbol}, headers)
        if oi.status == "ok" and isinstance(oi.body, dict) and oi.body.get("openInterest") is not None:
            stamp = _ms(oi.body.get("time")) or now
            rows.append(_obs("binance", "open_interest", symbol, _ASSETS[symbol], _num(oi.body.get("openInterest")), "contracts", stamp, now, 30))
        funding = await fetch_json(
            self.client,
            self.breaker,
            self.bucket,
            f"{base}/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 1},
            headers,
        )
        if funding.status == "ok" and isinstance(funding.body, list) and funding.body:
            last = funding.body[-1]
            if isinstance(last, dict) and last.get("fundingRate") is not None:
                stamp = _ms(last.get("fundingTime")) or now
                rows.append(_obs("binance", "funding_rate", symbol, _ASSETS[symbol], _num(last.get("fundingRate")), "rate", stamp, now, 28_800))
        return rows

    async def _keyed(self, base: str, symbol: str, now: datetime, headers: dict) -> tuple[list[RawExternalObservation], bool]:
        rows: list[RawExternalObservation] = []
        blocked = False
        specs = (
            ("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 12}, _oi_hist),
            ("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": 3}, _taker),
            ("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "5m", "limit": 1}, _account_ratio("global_long_short_ratio")),
            ("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "5m", "limit": 1}, _account_ratio("top_trader_account_ratio")),
            ("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "5m", "limit": 1}, _account_ratio("top_trader_position_ratio")),
        )
        for path, params, parser in specs:
            result = await fetch_json(self.client, self.breaker, self.bucket, f"{base}{path}", params, headers)
            if result.status == "GEO_BLOCKED":
                blocked = True
                continue
            if result.status != "ok" or not isinstance(result.body, list):
                continue
            rows.extend(parser(symbol, result.body, now))
        return rows, blocked


def _oi_hist(symbol: str, body: list, now: datetime) -> list[RawExternalObservation]:
    rows = []
    for item in body:
        if not isinstance(item, dict):
            continue
        stamp = _ms(item.get("timestamp")) or now
        if item.get("sumOpenInterest") is not None:
            rows.append(_obs("binance", "open_interest", symbol, _ASSETS[symbol], _num(item.get("sumOpenInterest")), "contracts", stamp, now, 300))
        if item.get("sumOpenInterestValue") is not None:
            rows.append(_obs("binance", "open_interest_value", symbol, _ASSETS[symbol], _num(item.get("sumOpenInterestValue")), "usd", stamp, now, 300))
    return rows


def _taker(symbol: str, body: list, now: datetime) -> list[RawExternalObservation]:
    rows = []
    for item in body:
        if not isinstance(item, dict):
            continue
        stamp = _ms(item.get("timestamp")) or now
        if item.get("buyVol") is not None:
            rows.append(_obs("binance", "taker_buy_volume", symbol, _ASSETS[symbol], _num(item.get("buyVol")), "contracts", stamp, now, 300))
        if item.get("sellVol") is not None:
            rows.append(_obs("binance", "taker_sell_volume", symbol, _ASSETS[symbol], _num(item.get("sellVol")), "contracts", stamp, now, 300))
        if item.get("buySellRatio") is not None:
            rows.append(_obs("binance", "taker_buy_sell_ratio", symbol, _ASSETS[symbol], _num(item.get("buySellRatio")), "ratio", stamp, now, 300))
    return rows


def _account_ratio(metric: str):
    def parse(symbol: str, body: list, now: datetime) -> list[RawExternalObservation]:
        rows = []
        for item in body:
            if isinstance(item, dict) and item.get("longShortRatio") is not None:
                stamp = _ms(item.get("timestamp")) or now
                rows.append(
                    _obs(
                        "binance",
                        metric,
                        symbol,
                        _ASSETS[symbol],
                        _num(item.get("longShortRatio")),
                        "ratio",
                        stamp,
                        now,
                        300,
                        {"long_account": item.get("longAccount"), "short_account": item.get("shortAccount")},
                    )
                )
        return rows

    return parse


def _obs(provider, metric, symbol, asset, value, unit, observed, received, frequency, metadata=None) -> RawExternalObservation:
    return RawExternalObservation(
        provider=provider,
        metric=metric,
        symbol=symbol,
        asset=asset,
        value=value,
        unit=unit,
        observed_at=observed,
        received_at=received,
        provider_timestamp=observed,
        source_frequency_seconds=frequency,
        metadata=metadata or {},
    )


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _ms(value) -> datetime | None:
    number = _num(value)
    if number is None:
        return None
    return datetime.fromtimestamp(number / 1000, tz=timezone.utc)
