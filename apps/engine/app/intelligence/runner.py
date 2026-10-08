from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.intelligence.context import build_context
from app.intelligence.providers import (
    AlternativeMeProvider,
    BinanceIntelligenceProvider,
    CoinMetricsProvider,
    CoinalyzeProvider,
    CryptoQuantProvider,
)
from app.intelligence.schemas import RawExternalObservation

log = logging.getLogger(__name__)

_FOCUS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


class IntelligenceCache:
    def __init__(self) -> None:
        self.rows: dict[tuple, RawExternalObservation] = {}
        self.health: dict[str, str] = {}

    def add(self, rows: list[RawExternalObservation]) -> None:
        for row in rows:
            if row.observed_at.tzinfo is None:
                continue
            key = (row.provider, row.metric, row.symbol, row.observed_at.isoformat())
            self.rows[key] = row

    def visible(self, now: datetime) -> list[RawExternalObservation]:
        cutoff = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return [row for row in self.rows.values() if row.observed_at <= cutoff]


class IntelligenceRunner:
    """Polls external sources beside the book. A failed provider does not stop the others."""

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.cache = IntelligenceCache()
        self.binance = BinanceIntelligenceProvider(settings, client)
        self.coinalyze = CoinalyzeProvider(settings, client)
        self.alternative = AlternativeMeProvider(settings, client)
        self.cryptoquant = CryptoQuantProvider(settings, client)
        self.coinmetrics = CoinMetricsProvider(settings, client)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def bind(self, client) -> None:
        self.binance.client = client
        self.coinalyze.client = client
        self.alternative.client = client
        self.cryptoquant.client = client
        self.coinmetrics.client = client

    def context_for(self, symbol: str, features: dict, now: datetime) -> dict | None:
        if not self.settings.external_intelligence_enabled:
            return None
        try:
            payload = build_context(symbol, features, self.cache.visible(now), now)
        except Exception:
            log.exception("intelligence context failed for %s", symbol)
            return None
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        payload["context_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
        gate = self.settings.jev_intelligence_gate
        overall = payload["data_quality"]["overall"]
        if gate == "NO_TRADE" and overall < self.settings.min_jev_data_quality:
            payload["gate"] = "NO_TRADE"
        else:
            payload["gate"] = "FALLBACK_TO_BASELINE"
        return payload

    async def start(self) -> None:
        if not self.settings.external_intelligence_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="external_intelligence")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def collect_once(self, now: datetime | None = None) -> None:
        stamp = now or datetime.now(timezone.utc)
        symbols = [symbol for symbol in self.settings.symbol_list if symbol in _FOCUS] or list(_FOCUS)
        for provider in (self.binance, self.coinalyze, self.alternative, self.cryptoquant, self.coinmetrics):
            try:
                rows = await provider.collect(symbols, stamp)
                health = await provider.health()
            except Exception as exc:
                log.info("provider=%s status=ERROR detail=%s", provider.name, type(exc).__name__)
                self.cache.health[provider.name] = "ERROR"
                continue
            self.cache.add(rows)
            self.cache.health[provider.name] = health.status

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.collect_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                continue
