from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.intelligence.schemas import ProviderCapability, ProviderHealth, RawExternalObservation


class CryptoQuantProvider:
    """No metric endpoint is called until a catalog for this key says it exists."""

    name = "cryptoquant"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.capabilities: list[ProviderCapability] = []

    async def health(self) -> ProviderHealth:
        if not self.settings.cryptoquant_enabled or not self.settings.cryptoquant_api_key:
            return ProviderHealth(provider=self.name, status="DISABLED")
        return ProviderHealth(provider=self.name, status="DEGRADED", last_error="CATALOG_NOT_DISCOVERED")

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        return []


class CoinMetricsProvider:
    """Community catalog only. A metric is usable after it appears for that asset."""

    name = "coinmetrics"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.capabilities: list[ProviderCapability] = []
        self._status = "DISABLED"
        self._error: str | None = None

    async def health(self) -> ProviderHealth:
        if not self.settings.coinmetrics_enabled:
            return ProviderHealth(provider=self.name, status="DISABLED")
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        if not self.settings.coinmetrics_enabled:
            self._status = "DISABLED"
            return []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return []
        host = self.settings.coinmetrics_base_host.strip().removeprefix("https://").strip("/")
        params = {"assets": "btc,eth,sol"}
        headers = {}
        if self.settings.coinmetrics_api_key:
            params["api_key"] = self.settings.coinmetrics_api_key
        try:
            response = await self.client.get(f"https://{host}/v4/catalog-all-v2/asset-metrics", params=params, headers=headers, timeout=8)
        except Exception as exc:
            self._status = "ERROR"
            self._error = type(exc).__name__
            return []
        if response.status_code >= 400:
            self._status = "ERROR"
            self._error = f"HTTP {response.status_code}"
            return []
        try:
            body = response.json()
        except Exception:
            self._status = "ERROR"
            self._error = "INVALID_JSON"
            return []
        self.capabilities = _capabilities(body, now)
        self._status = "ONLINE" if self.capabilities else "DEGRADED"
        self._error = None if self.capabilities else "EMPTY_CATALOG"
        return []


def _capabilities(body, now: datetime) -> list[ProviderCapability]:
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    found = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        asset = item.get("asset")
        frequencies = item.get("frequencies")
        metrics = item.get("metrics")
        if isinstance(metrics, list):
            for metric in metrics:
                name = metric.get("metric") if isinstance(metric, dict) else metric
                if not name:
                    continue
                found.append(
                    ProviderCapability(
                        provider="coinmetrics",
                        metric=str(name),
                        asset=str(asset).upper() if asset else None,
                        frequency=str(frequencies[0]) if isinstance(frequencies, list) and frequencies else None,
                        available=True,
                        last_checked_at=now,
                    )
                )
    return found
