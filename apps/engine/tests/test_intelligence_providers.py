from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.intelligence.context import build_context
from app.intelligence.providers.binance import BinanceIntelligenceProvider
from app.intelligence.providers.coinalyze import CoinalyzeProvider
from app.intelligence.providers.alternative import AlternativeMeProvider
from app.intelligence.runner import IntelligenceRunner
from app.intelligence.schemas import RawExternalObservation
from tests.conftest import settings


def _now() -> datetime:
    return datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_binance_reads_open_interest_and_sends_the_key_only_on_market_data():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-MBX-APIKEY")))
        assert "order" not in request.url.path
        if request.url.path == "/fapi/v1/openInterest":
            return httpx.Response(200, json={"openInterest": "10.5", "symbol": "BTCUSDT", "time": 1_759_900_000_000})
        if request.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[{"fundingRate": "0.0001", "fundingTime": 1_759_900_000_000}])
        if request.url.path == "/futures/data/takerlongshortRatio":
            return httpx.Response(
                200,
                json=[{"buySellRatio": "1.5", "buyVol": "30", "sellVol": "20", "timestamp": 1_759_900_000_000}],
            )
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(binance_api_key="k", binance_api_secret="s", binance_futures_rest_url="https://fapi.binance.com")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceIntelligenceProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert ("/fapi/v1/openInterest", "k") in seen
    assert ("/futures/data/takerlongshortRatio", "k") in seen
    metrics = {row.metric: row.value for row in rows}
    assert metrics["open_interest"] == 10.5
    assert metrics["funding_rate"] == 0.0001
    assert metrics["taker_buy_volume"] == 30
    assert metrics["taker_sell_volume"] == 20


@pytest.mark.asyncio
async def test_coinalyze_uses_the_catalog_symbol_and_a_500_does_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api_key"] == "secret"
        assert request.url.path == "/v1/future-markets"
        return httpx.Response(500, json={"error": "down"})

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT", "SOLUSDT"], _now())
    assert rows == []
    assert (await provider.health()).status == "ERROR"
    assert provider.mappings() == []


@pytest.mark.asyncio
async def test_coinalyze_keeps_the_symbol_the_catalog_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/future-markets":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT_PERP.A",
                        "exchange": "A",
                        "base_asset": "BTC",
                        "quote_asset": "USDT",
                        "is_perpetual": True,
                        "margined": "STABLE",
                        "has_long_short_ratio_data": True,
                        "has_buy_sell_data": True,
                    }
                ],
            )
        if request.url.path == "/v1/open-interest":
            assert request.url.params["symbols"] == "BTCUSDT_PERP.A"
            return httpx.Response(200, json=[{"symbol": "BTCUSDT_PERP.A", "value": 1000, "update": 1_759_900_000}])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert provider.mappings()[0]["provider_symbol"] == "BTCUSDT_PERP.A"
    assert any(row.metric == "aggregate_open_interest_usd" and row.value == 1000 for row in rows)


@pytest.mark.asyncio
async def test_alternative_429_does_not_invent_fear_and_greed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9"})

    transport = httpx.MockTransport(handler)
    cfg = settings(alternative_me_enabled=True)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = AlternativeMeProvider(cfg, client)
        rows = await provider.collect(["SOLUSDT"], _now())
    assert rows == []
    assert (await provider.health()).status == "DEGRADED"


@pytest.mark.asyncio
async def test_one_provider_failing_does_not_drop_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.coinalyze.net":
            return httpx.Response(500, json={})
        if host == "api.alternative.me":
            return httpx.Response(429, headers={"Retry-After": "3"})
        if request.url.path == "/fapi/v1/openInterest":
            return httpx.Response(200, json={"openInterest": "4", "time": 1_759_900_000_000})
        if request.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(
        coinalyze_enabled=True,
        coinalyze_api_key="secret",
        alternative_me_enabled=True,
        cryptoquant_enabled=True,
        cryptoquant_api_key="cq",
        binance_api_key="",
        external_intelligence_enabled=True,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        runner = IntelligenceRunner(cfg, client)

        async def timeout(symbols, now):
            raise TimeoutError("cryptoquant")

        runner.cryptoquant.collect = timeout
        await runner.collect_once(_now())
    assert runner.cache.health["coinalyze"] == "ERROR"
    assert runner.cache.health["alternative_me"] == "DEGRADED"
    assert runner.cache.health["cryptoquant"] == "ERROR"
    assert any(row.metric == "open_interest" and row.value == 4 for row in runner.cache.rows.values())


def test_fear_and_greed_keeps_the_path_and_liquidation_missing_is_null():
    now = _now()
    rows = [
        RawExternalObservation(
            provider="alternative_me",
            metric="fear_greed",
            asset="BTC",
            value=value,
            unit="index",
            observed_at=now - timedelta(days=days),
            received_at=now,
            metadata={"scope": "BTC_MARKET_SENTIMENT", "classification": "Fear"},
        )
        for days, value in ((7, 8), (1, 8), (0, 20))
    ]
    context = build_context("SOLUSDT", {"imbalance_5": 0.2, "taker_flow_1m": 0.1, "spread_bps": 1.5}, rows, now)
    fear = context["sentiment"]["fear_greed"]
    assert fear["scope"] == "BTC_MARKET_SENTIMENT"
    assert fear["raw"] == 20
    assert fear["signed"] == -0.6
    assert fear["change_1d"] == 12
    assert fear["change_7d"] == 12
    assert context["derivatives"]["liquidation_imbalance"] is None
    assert context["onchain"]["exchange_flow_pressure"] is None
    assert context["onchain"]["available"] is False
    later = rows + [
        RawExternalObservation(
            provider="coinalyze",
            metric="long_liquidations",
            symbol="SOLUSDT",
            value=99,
            unit="contracts",
            observed_at=now + timedelta(seconds=1),
            received_at=now,
        )
    ]
    blocked = build_context("SOLUSDT", {}, later, now)
    assert blocked["derivatives"]["liquidation_imbalance"] is None
