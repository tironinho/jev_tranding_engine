"""Reserve provider capacity for active signals; sample live research separately."""
from __future__ import annotations

import asyncio
import time

from app.providers.jev.schemas import JevProviderError, JevShadowDeferred


class JevScheduler:
    def __init__(self, *, active_timeout_s=6.0, shadow_timeout_s=4.0, shadow_interval_s=120.0):
        self.active_timeout_s = active_timeout_s
        self.shadow_timeout_s = shadow_timeout_s
        self.shadow_interval_s = shadow_interval_s
        self._active_slots = asyncio.Semaphore(3)
        self._active_pending = 0
        self._shadow_running = False
        self._last_shadow: dict[str, float] = {}

    async def evaluate(self, provider, request, *, shadow=False, deadline=None, telemetry=None):
        telemetry = telemetry if telemetry is not None else {}
        queued_at = time.monotonic()
        if shadow:
            # Give active requests from the same event-loop burst first choice.
            await asyncio.sleep(0)
            now = time.monotonic()
            if self._active_pending or self._shadow_running:
                raise JevShadowDeferred("ACTIVE_PRIORITY_OR_SHADOW_BUSY")
            previous = self._last_shadow.get(request.symbol)
            if previous is not None and now - previous < self.shadow_interval_s:
                raise JevShadowDeferred("SHADOW_SAMPLE_INTERVAL")
            self._shadow_running = True
            self._last_shadow[request.symbol] = now
            try:
                return await self._call(provider, request, self.shadow_timeout_s, deadline, telemetry)
            finally:
                self._shadow_running = False
        self._active_pending += 1
        try:
            # Active slots never depend on the shadow slot.
            async with self._active_slots:
                telemetry["queue_ms"] = (time.monotonic() - queued_at) * 1000
                return await self._call(provider, request, self.active_timeout_s, deadline, telemetry)
        finally:
            self._active_pending -= 1

    async def _call(self, provider, request, configured, deadline, telemetry=None):
        timeout = configured
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic() - 2.0)
        if timeout <= 0:
            raise JevProviderError("jev_budget_exhausted")
        request = request.model_copy(update={"evaluation_timeout_s": timeout})
        started = time.monotonic()
        if telemetry is not None:
            telemetry["timeout_s"] = timeout
        try:
            return await asyncio.wait_for(provider.evaluate_market_state(request), timeout=timeout)
        except Exception as exc:
            if telemetry is not None:
                telemetry["failure_type"] = type(exc).__name__
                telemetry["transport_failure_type"] = type(exc.__cause__).__name__ if exc.__cause__ else None
            raise
        finally:
            if telemetry is not None:
                telemetry["provider_total_ms"] = (time.monotonic() - started) * 1000

    async def research_batch(self, operation):
        """Low-priority batch; active order decisions never wait on this slot."""
        await asyncio.sleep(0)
        if self._active_pending or self._shadow_running:
            raise JevShadowDeferred("ACTIVE_PRIORITY_OR_SHADOW_BUSY")
        self._shadow_running = True
        try:
            return await operation()
        finally:
            self._shadow_running = False

    async def research_assessment(self, provider, request):
        if self._active_pending:
            raise JevShadowDeferred("ACTIVE_PRIORITY")
        return await self._call(provider, request, self.shadow_timeout_s, None)
