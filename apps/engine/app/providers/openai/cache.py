from __future__ import annotations

from typing import Protocol

from app.providers.openai.schemas import MarketState


class MarketStateCache(Protocol):
    async def get(self, key: str) -> MarketState | None: ...

    async def set(self, key: str, value: MarketState) -> None: ...


class DisabledMarketStateCache:
    """Caching stays off until there is a measured reason to turn it on."""

    enabled = False

    async def get(self, key: str) -> MarketState | None:
        return None

    async def set(self, key: str, value: MarketState) -> None:
        return None
