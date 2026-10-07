from __future__ import annotations

import hashlib
import hmac
import time
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


class BinanceFeeProvider:
    """Uses USD-M GET /fapi/v1/commissionRate when keys exist. Any failure falls back to config."""

    def __init__(self, settings: Settings, fees: FeeConfig, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.fallback = ConfigFeeProvider(fees)
        self.client = client
        self.breaker = CircuitBreaker("binance_fees", failure_threshold=3, recovery_seconds=120)
        self.bucket = TokenBucket(rate=0.2, capacity=2)
        self._cache: dict[str, FeeQuote] = {}

    async def get_fees(self, symbol: str) -> FeeQuote:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        if (
            self.settings.market_type != "futures"
            or not self.settings.binance_api_key
            or not self.settings.binance_api_secret
            or self.client is None
            or not self.breaker.allow()
            or not self.bucket.take()
        ):
            return await self.fallback.get_fees(symbol)
        params = {"symbol": symbol, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        signature = hmac.new(self.settings.binance_api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            response = await self.client.get(
                f"{self.settings.binance_futures_rest_url}/fapi/v1/commissionRate",
                params={**params, "signature": signature},
                headers={"X-MBX-APIKEY": self.settings.binance_api_key},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            quote = FeeQuote(
                maker=float(payload["makerCommissionRate"]),
                taker=float(payload["takerCommissionRate"]),
                source="binance",
            )
        except Exception:
            self.breaker.record_failure()
            return await self.fallback.get_fees(symbol)
        self.breaker.record_success()
        self._cache[symbol] = quote
        return quote
