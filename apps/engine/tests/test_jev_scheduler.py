import asyncio
import time

import pytest

from app.providers.jev.mock import MockJevProvider
from app.providers.jev.scheduler import JevScheduler
from app.providers.jev.schemas import JevMarketRequest, JevProviderError, JevShadowDeferred
from tests.conftest import clock


def request(symbol="BTCUSDT"):
    return JevMarketRequest(prompt_version="test", symbol=symbol, market_type="margin",
        timestamp=clock(), features={}, baseline_action="SHORT")


@pytest.mark.asyncio
async def test_active_request_has_capacity_while_shadow_is_running():
    shadow_started = asyncio.Event()
    active_started = asyncio.Event()
    release = asyncio.Event()

    class Provider(MockJevProvider):
        async def evaluate_market_state(self, req):
            (shadow_started if req.symbol == "SOLUSDT" else active_started).set()
            await release.wait()
            return await super().evaluate_market_state(req)

    provider = Provider("test")
    scheduler = JevScheduler()
    shadow = asyncio.create_task(scheduler.evaluate(provider, request("SOLUSDT"), shadow=True))
    await asyncio.wait_for(shadow_started.wait(), 1)
    active = asyncio.create_task(scheduler.evaluate(provider, request()))
    await asyncio.wait_for(active_started.wait(), 1)
    release.set()
    await asyncio.gather(active, shadow)


@pytest.mark.asyncio
async def test_shadow_is_deferred_when_active_signal_is_pending():
    started, release = asyncio.Event(), asyncio.Event()

    class Provider(MockJevProvider):
        calls = 0

        async def evaluate_market_state(self, req):
            self.calls += 1
            started.set()
            await release.wait()
            return await super().evaluate_market_state(req)

    provider = Provider("test")
    scheduler = JevScheduler()
    active = asyncio.create_task(scheduler.evaluate(provider, request()))
    await asyncio.wait_for(started.wait(), 1)
    with pytest.raises(JevShadowDeferred, match="ACTIVE_PRIORITY"):
        await scheduler.evaluate(provider, request("ETHUSDT"), shadow=True)
    assert provider.calls == 1
    release.set()
    await active


@pytest.mark.asyncio
async def test_shadow_sampling_never_throttles_an_active_request():
    scheduler = JevScheduler()
    provider = MockJevProvider("test")
    await scheduler.evaluate(provider, request(), shadow=True)
    with pytest.raises(JevShadowDeferred, match="SAMPLE_INTERVAL"):
        await scheduler.evaluate(provider, request(), shadow=True)
    assert await scheduler.evaluate(provider, request())


@pytest.mark.asyncio
async def test_timeout_respects_remaining_budget_and_stays_off_model_payload():
    class Provider(MockJevProvider):
        seen = None

        async def evaluate_market_state(self, req):
            self.seen = req
            return await super().evaluate_market_state(req)

    provider = Provider("test")
    scheduler = JevScheduler(active_timeout_s=6)
    await scheduler.evaluate(provider, request(), deadline=time.monotonic()+5)
    assert 2.5 < provider.seen.evaluation_timeout_s <= 3
    assert "evaluation_timeout_s" not in provider.seen.model_dump()
    with pytest.raises(JevProviderError, match="budget_exhausted"):
        await scheduler.evaluate(provider, request(), deadline=time.monotonic()+1)


@pytest.mark.asyncio
async def test_cancelled_shadow_releases_capacity():
    started = asyncio.Event()

    class Provider(MockJevProvider):
        async def evaluate_market_state(self, req):
            started.set()
            await asyncio.Event().wait()

    scheduler = JevScheduler(shadow_interval_s=0)
    task = asyncio.create_task(scheduler.evaluate(Provider("test"), request(), shadow=True))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await scheduler.evaluate(MockJevProvider("test"), request(), shadow=True)
