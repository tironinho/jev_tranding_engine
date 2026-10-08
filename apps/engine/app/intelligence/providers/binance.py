from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.intelligence.http import fetch_json
from app.intelligence.schemas import ProviderHealth, RawExternalObservation
from app.providers.binance_account import _signed
from app.resilience.guards import CircuitBreaker, TokenBucket

_ASSETS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "BNBUSDT": "BNB", "XRPUSDT": "XRP"}
_COINS = set(_ASSETS.values()) | {"USDT"}


class BinanceIntelligenceProvider:
    """Margin borrow and price index, or futures derivatives when the book is futures.

    The spot book already feeds price, depth and trades. A margin book never calls fapi.
    """

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
        if self.settings.market_type != "futures":
            rows, blocked = await self._margin(wanted, now, headers)
            self._status = "DEGRADED" if blocked and not rows else "ONLINE" if rows or not wanted else "DEGRADED"
            self._error = "HTTP 451" if blocked and not rows else None
            return rows
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

    async def _margin(self, symbols: list[str], now: datetime, headers: dict) -> tuple[list[RawExternalObservation], bool]:
        """Cross-margin price index and borrow interest. No futures host."""
        if self.settings.balance_upstream_url:
            return await self._margin_via_account(symbols, now), False
        base = self.settings.binance_account_rest_url.rstrip("/")
        if not base.startswith("http"):
            self._error = "NO_ACCOUNT_URL"
            return [], False
        rows: list[RawExternalObservation] = []
        blocked = False
        for symbol in symbols:
            index = await fetch_json(
                self.client,
                self.breaker,
                self.bucket,
                f"{base}/sapi/v1/margin/priceIndex",
                {"symbol": symbol},
                headers,
            )
            if index.status == "GEO_BLOCKED":
                blocked = True
                continue
            if index.status == "ok" and isinstance(index.body, dict) and index.body.get("price") is not None:
                stamp = _ms(index.body.get("calcTime")) or now
                rows.append(_obs("binance", "margin_price_index", symbol, _ASSETS[symbol], _num(index.body.get("price")), "usd", stamp, now, 60))
        if not self.settings.binance_api_secret:
            return rows, blocked
        assets = [_ASSETS[symbol] for symbol in symbols]
        if "USDT" not in assets:
            assets.append("USDT")
        signed = _signed(
            self.settings.binance_api_secret,
            {"assets": ",".join(assets), "isIsolated": "FALSE", "timestamp": int(now.timestamp() * 1000), "recvWindow": 5000},
        )
        hourly = await fetch_json(
            self.client,
            self.breaker,
            self.bucket,
            f"{base}/sapi/v1/margin/next-hourly-interest-rate",
            signed,
            headers,
        )
        if hourly.status == "GEO_BLOCKED":
            blocked = True
        elif hourly.status == "ok" and isinstance(hourly.body, list):
            rows.extend(_hourly_interest(symbols, hourly.body, now))
        for asset in assets:
            daily = await fetch_json(
                self.client,
                self.breaker,
                self.bucket,
                f"{base}/sapi/v1/margin/crossMarginData",
                _signed(self.settings.binance_api_secret, {"coin": asset, "timestamp": int(now.timestamp() * 1000), "recvWindow": 5000}),
                headers,
            )
            if daily.status == "GEO_BLOCKED":
                blocked = True
                continue
            if daily.status == "ok" and isinstance(daily.body, list):
                rows.extend(_daily_interest(symbols, asset, daily.body, now))
        return rows, blocked

    async def _margin_via_account(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        """Oregon cannot open sapi. Singapore reads the price index and the borrow rate."""
        token = self.settings.balance_share_token or self.settings.engine_api_secret
        if not token or self.client is None:
            return []
        url = f"{self.settings.balance_upstream_url.rstrip('/')}/api/binance/margin-read"
        rows: list[RawExternalObservation] = []
        for symbol in symbols:
            body = await self._account_body(url, token, {"kind": "price_index", "symbol": symbol})
            if isinstance(body, dict) and body.get("price") is not None:
                stamp = _ms(body.get("calcTime")) or now
                rows.append(_obs("binance", "margin_price_index", symbol, _ASSETS[symbol], _num(body.get("price")), "usd", stamp, now, 60))
        assets = [_ASSETS[symbol] for symbol in symbols]
        if "USDT" not in assets:
            assets.append("USDT")
        hourly = await self._account_body(url, token, {"kind": "hourly_interest", "assets": assets})
        if isinstance(hourly, list):
            rows.extend(_hourly_interest(symbols, hourly, now))
        for asset in assets:
            daily = await self._account_body(url, token, {"kind": "daily_interest", "asset": asset})
            if isinstance(daily, list):
                rows.extend(_daily_interest(symbols, asset, daily, now))
        return rows

    async def _account_body(self, url: str, token: str, payload: dict):
        try:
            response = await self.client.post(
                url,
                json=payload,
                headers={"authorization": f"Bearer {token}"},
                timeout=8,
            )
        except Exception as exc:
            self._error = type(exc).__name__
            return None
        if response.status_code != 200:
            self._error = f"HTTP {response.status_code}"
            return None
        try:
            body = response.json()
        except Exception:
            self._error = "INVALID_JSON"
            return None
        if not isinstance(body, dict) or body.get("status") != "ok":
            self._error = str(body.get("detail") or body.get("status") or "ERROR") if isinstance(body, dict) else "ERROR"
            return None
        self._error = None
        return body.get("body")


async def read_margin_on_account(client, settings: Settings, kind: str, symbol: str | None, asset: str | None, assets: list[str] | None) -> dict:
    """Signed read on the account host. The path is chosen here, never by the caller."""
    if kind not in {"price_index", "hourly_interest", "daily_interest"}:
        raise ValueError("kind")
    if client is None or not settings.binance_api_key or not settings.binance_api_secret:
        raise ValueError("NO_CREDENTIALS")
    base = settings.binance_account_rest_url.rstrip("/")
    if not base.startswith("http"):
        raise ValueError("NO_ACCOUNT_URL")
    headers = {"X-MBX-APIKEY": settings.binance_api_key}
    if kind == "price_index":
        if symbol not in _ASSETS:
            raise ValueError("symbol")
        path, params, signed = "/sapi/v1/margin/priceIndex", {"symbol": symbol}, False
    elif kind == "hourly_interest":
        coins = [item for item in (assets or []) if item in _COINS]
        if "USDT" not in coins:
            coins.append("USDT")
        path, params, signed = "/sapi/v1/margin/next-hourly-interest-rate", {"assets": ",".join(coins), "isIsolated": "FALSE"}, True
    elif kind == "daily_interest":
        if asset not in _COINS:
            raise ValueError("asset")
        path, params, signed = "/sapi/v1/margin/crossMarginData", {"coin": asset}, True
    else:
        raise ValueError("kind")
    if signed:
        params = _signed(
            settings.binance_api_secret,
            {**params, "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000), "recvWindow": 5000},
        )
    response = await client.get(f"{base}{path}", params=params, headers=headers, timeout=8)
    if response.status_code >= 400:
        return {"status": "ERROR", "detail": f"HTTP {response.status_code}"}
    try:
        body = response.json()
    except Exception:
        return {"status": "ERROR", "detail": "INVALID_JSON"}
    return {"status": "ok", "body": body}


def _hourly_interest(symbols: list[str], body: list, now: datetime) -> list[RawExternalObservation]:
    by_asset = {item.get("asset"): item for item in body if isinstance(item, dict)}
    rows: list[RawExternalObservation] = []
    quote = by_asset.get("USDT")
    quote_rate = _num(quote.get("nextHourlyInterestRate")) if quote else None
    for symbol in symbols:
        asset = _ASSETS[symbol]
        item = by_asset.get(asset)
        rate = _num(item.get("nextHourlyInterestRate")) if item else None
        if rate is not None:
            rows.append(_obs("binance", "borrow_hourly_interest", symbol, asset, rate, "rate", now, now, 3600))
        if quote_rate is not None:
            rows.append(_obs("binance", "quote_borrow_hourly_interest", symbol, "USDT", quote_rate, "rate", now, now, 3600))
    return rows


def _daily_interest(symbols: list[str], asset: str, body: list, now: datetime) -> list[RawExternalObservation]:
    rate = None
    for item in body:
        if isinstance(item, dict) and item.get("coin") == asset and item.get("dailyInterest") is not None:
            rate = _num(item.get("dailyInterest"))
            break
    if rate is None:
        return []
    rows = []
    if asset == "USDT":
        for symbol in symbols:
            rows.append(_obs("binance", "quote_borrow_daily_interest", symbol, "USDT", rate, "rate", now, now, 86_400))
        return rows
    for symbol in symbols:
        if _ASSETS[symbol] == asset:
            rows.append(_obs("binance", "borrow_daily_interest", symbol, asset, rate, "rate", now, now, 86_400))
    return rows


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
