from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import replace
from urllib.parse import urlencode

import httpx

from app.config import FeeConfig, Settings
from app.resilience.guards import CircuitBreaker, TokenBucket
from app.risk.engine import FeeQuote


class ConfigFeeProvider:
    def __init__(self, fees: FeeConfig) -> None:
        self.fees = fees

    async def get_fees(self, symbol: str) -> FeeQuote:
        return FeeQuote(maker=self.fees.maker_fee_rate, taker=self.fees.taker_fee_rate, source="config")

    def estimate(self, symbol: str) -> FeeQuote:
        return FeeQuote(maker=self.fees.maker_fee_rate, taker=self.fees.taker_fee_rate, source="config")


class BinanceFeeProvider:
    """Uses the account fee endpoint for futures or spot/margin, with a conservative fallback."""

    def __init__(self, settings: Settings, fees: FeeConfig, client: httpx.AsyncClient | None = None, *, ttl_s: float = 300) -> None:
        self.settings = settings
        # Unknown margin fees must not assume the unobserved BNB discount.
        if settings.market_type == "margin":
            fees = replace(fees, maker_fee_rate=max(fees.maker_fee_rate, .001),
                           taker_fee_rate=max(fees.taker_fee_rate, .001))
        self.fallback = ConfigFeeProvider(fees)
        self.client = client
        self.ttl_s = ttl_s
        self.breaker = CircuitBreaker("binance_fees", failure_threshold=3, recovery_seconds=120)
        self.bucket = TokenBucket(rate=0.2, capacity=2)
        self._cache: dict[str, FeeQuote] = {}
        self._cached_at: dict[str, float] = {}

    def estimate(self, symbol: str) -> FeeQuote:
        cached = self._cache.get(symbol)
        if cached is not None and time.monotonic() - self._cached_at.get(symbol, 0) < self.ttl_s:
            return cached
        return self.fallback.estimate(symbol)

    async def get_fees(self, symbol: str) -> FeeQuote:
        cached = self._cache.get(symbol)
        if cached is not None and time.monotonic() - self._cached_at.get(symbol, 0) < self.ttl_s:
            return cached
        relay = self.settings.market_type == "margin" and bool(self.settings.balance_upstream_url)
        if (
            self.client is None
            or (not relay and (not self.settings.binance_api_key or not self.settings.binance_api_secret))
            or not self.breaker.allow()
            or not self.bucket.take()
        ):
            return await self.fallback.get_fees(symbol)
        try:
            quote = await self._gateway_fees(symbol) if relay else await self._account_fees(symbol)
            if not all(math.isfinite(rate) and 0 <= rate <= .1 for rate in (quote.maker, quote.taker)):
                raise ValueError("invalid account fee")
        except Exception:
            self.breaker.record_failure()
            return await self.fallback.get_fees(symbol)
        self.breaker.record_success()
        self._cache[symbol] = quote
        self._cached_at[symbol] = time.monotonic()
        return quote

    async def _gateway_fees(self, symbol: str) -> FeeQuote:
        token = self.settings.balance_share_token or self.settings.engine_api_secret
        if not token:
            raise ValueError("gateway token missing")
        response = await self.client.get(
            f"{self.settings.balance_upstream_url.rstrip('/')}/api/binance/fees",
            params={"symbol": symbol}, headers={"authorization": f"Bearer {token}"}, timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        # A gateway fallback is useful to the caller but is not an account
        # observation and must not become a five-minute verified cache entry.
        if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("source") != "binance":
            raise ValueError("account fees unavailable")
        return FeeQuote(maker=_fee_rate(payload["maker"]), taker=_fee_rate(payload["taker"]), source="binance_gateway")

    async def _account_fees(self, symbol: str) -> FeeQuote:
        params = {"symbol": symbol, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        signature = hmac.new(self.settings.binance_api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        futures = self.settings.market_type == "futures"
        account_url = (self.settings.binance_account_rest_url if self.settings.market_type == "margin"
                       else self.settings.binance_spot_rest_url)
        url = (f"{self.settings.binance_futures_rest_url}/fapi/v1/commissionRate" if futures
               else f"{account_url.rstrip('/')}/sapi/v1/asset/tradeFee")
        response = await self.client.get(url, params={**params, "signature": signature},
                                         headers={"X-MBX-APIKEY": self.settings.binance_api_key}, timeout=5)
        response.raise_for_status()
        payload = response.json()
        if not futures:
            payload = next((item for item in payload if item.get("symbol") == symbol), None) if isinstance(payload, list) else payload
        if not isinstance(payload, dict):
            raise ValueError("account fee missing")
        return FeeQuote(
            maker=_fee_rate(payload["makerCommissionRate" if futures else "makerCommission"]),
            taker=_fee_rate(payload["takerCommissionRate" if futures else "takerCommission"]),
            source="binance",
        )


def _fee_rate(value) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid account fee")
    rate = float(value)
    if not math.isfinite(rate) or not 0 <= rate <= .1:
        raise ValueError("invalid account fee")
    return rate
