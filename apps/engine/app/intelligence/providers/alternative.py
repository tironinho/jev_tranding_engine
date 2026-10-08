from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.intelligence.http import fetch_json
from app.intelligence.schemas import ProviderHealth, RawExternalObservation
from app.resilience.guards import CircuitBreaker, TokenBucket


class AlternativeMeProvider:
    """Fear & Greed is daily Bitcoin context. It is not a SOL or ETH sentiment print."""

    name = "alternative_me"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.breaker = CircuitBreaker("alternative_me", failure_threshold=4, recovery_seconds=120)
        self.bucket = TokenBucket(rate=1 / 30, capacity=2)
        self._status = "DISABLED"
        self._error: str | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        if not self.settings.alternative_me_enabled:
            self._status = "DISABLED"
            return []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return []
        host = self.settings.alternative_me_base_host.strip().removeprefix("https://").strip("/")
        base = f"https://{host}"
        rows: list[RawExternalObservation] = []
        fng = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/fng/", {"limit": 90, "format": "json"}, {})
        if fng.status == "RATE_LIMIT":
            self._status = "DEGRADED"
            self._error = "429"
            return []
        if fng.status == "ok":
            rows.extend(_fear(fng.body, now))
        global_market = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/v2/global/", {}, {})
        if global_market.status == "ok":
            rows.extend(_global(global_market.body, now))
        elif global_market.status == "RATE_LIMIT":
            self._status = "DEGRADED"
            self._error = "429"
            return rows
        self._status = "ONLINE" if rows else "DEGRADED"
        self._error = None if rows else fng.status
        return rows


def _fear(body, now: datetime) -> list[RawExternalObservation]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        try:
            value = float(item["value"])
        except (TypeError, ValueError):
            continue
        stamp = _seconds(item.get("timestamp")) or now
        rows.append(
            RawExternalObservation(
                provider="alternative_me",
                metric="fear_greed",
                symbol=None,
                asset="BTC",
                value=value,
                unit="index",
                observed_at=stamp,
                received_at=now,
                provider_timestamp=stamp,
                source_frequency_seconds=86_400,
                metadata={
                    "scope": "BTC_MARKET_SENTIMENT",
                    "classification": item.get("value_classification"),
                },
            )
        )
    return rows


def _global(body, now: datetime) -> list[RawExternalObservation]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("bitcoin_percentage_of_market_cap") is None:
        return []
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    quotes = data.get("quotes") if isinstance(data.get("quotes"), dict) else {}
    usd = quotes.get("USD") if isinstance(quotes.get("USD"), dict) else {}
    rows = [
        RawExternalObservation(
            provider="alternative_me",
            metric="btc_dominance",
            symbol=None,
            asset="BTC",
            value=float(data["bitcoin_percentage_of_market_cap"]),
            unit="percent",
            observed_at=stamp,
            received_at=now,
            provider_timestamp=stamp,
            source_frequency_seconds=900,
            metadata={"scope": "GLOBAL_MARKET"},
        )
    ]
    if usd.get("total_market_cap") is not None:
        rows.append(
            RawExternalObservation(
                provider="alternative_me",
                metric="total_market_cap",
                symbol=None,
                asset=None,
                value=float(usd["total_market_cap"]),
                unit="usd",
                observed_at=stamp,
                received_at=now,
                provider_timestamp=stamp,
                source_frequency_seconds=900,
            )
        )
    if usd.get("total_volume_24h") is not None:
        rows.append(
            RawExternalObservation(
                provider="alternative_me",
                metric="total_volume_24h",
                symbol=None,
                asset=None,
                value=float(usd["total_volume_24h"]),
                unit="usd",
                observed_at=stamp,
                received_at=now,
                provider_timestamp=stamp,
                source_frequency_seconds=900,
            )
        )
    return rows


def _seconds(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
