from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.intelligence.schemas import ProviderHealth, RawExternalObservation


class ExternalDataProvider(Protocol):
    name: str

    async def health(self) -> ProviderHealth: ...

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]: ...


class DisabledProvider:
    """Enabled flag or missing key. Collects nothing and does not raise."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, status="DISABLED")

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        return []
