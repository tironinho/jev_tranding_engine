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

    def estimate(self, symbol: str) -> FeeQuote:
        return FeeQuote(maker=self.fees.maker_fee_rate, taker=self.fees.taker_fee_rate, source="config")


class BinanceFeeProvider:
    """Uses the account fee endpoint for futures or spot/margin, with a conservative fallback."""

    def __init__(self, settings: Settings, fees: FeeConfig, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.fallback = ConfigFeeProvider(fees)
        self.client = client
        self.breaker = CircuitBreaker("binance_fees", failure_threshold=3, recovery_seconds=120)
        self.bucket = TokenBucket(rate=0.2, capacity=2)
        self._cache: dict[str, FeeQuote] = {}

    def estimate(self, symbol: str) -> FeeQuote:
        return self._cache.get(symbol) or self.fallback.estimate(symbol)

    async def get_fees(self, symbol: str) -> FeeQuote:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        if (
            not self.settings.binance_api_key
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
            futures = self.settings.market_type == "futures"
            url = (f"{self.settings.binance_futures_rest_url}/fapi/v1/commissionRate" if futures
                   else f"{self.settings.binance_spot_rest_url}/sapi/v1/asset/tradeFee")
            response = await self.client.get(url, params={**params, "signature": signature},
                                             headers={"X-MBX-APIKEY": self.settings.binance_api_key}, timeout=5)
            response.raise_for_status()
            payload = response.json()
            if not futures:
                payload = next((item for item in payload if item.get("symbol") == symbol), None) if isinstance(payload, list) else payload
                if not isinstance(payload, dict):
                    raise ValueError("spot fee missing")
            quote = FeeQuote(
                maker=float(payload["makerCommissionRate" if futures else "makerCommission"]),
                taker=float(payload["takerCommissionRate" if futures else "takerCommission"]),
                source="binance",
            )
        except Exception:
            self.breaker.record_failure()
            return await self.fallback.get_fees(symbol)
        self.breaker.record_success()
        self._cache[symbol] = quote
        return quote
