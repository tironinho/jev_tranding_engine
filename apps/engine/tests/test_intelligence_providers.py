import json
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
async def test_margin_reads_borrow_interest_and_never_calls_futures():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert "/fapi/" not in request.url.path
        assert "/futures/" not in request.url.path
        if request.url.path == "/sapi/v1/margin/priceIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "64000", "calcTime": 1_759_900_000_000})
        if request.url.path == "/sapi/v1/margin/next-hourly-interest-rate":
            assert "signature" in request.url.params
            return httpx.Response(
                200,
                json=[
                    {"asset": "BTC", "nextHourlyInterestRate": "0.000008"},
                    {"asset": "USDT", "nextHourlyInterestRate": "0.00001"},
                ],
            )
        if request.url.path == "/sapi/v1/margin/crossMarginData":
            coin = request.url.params["coin"]
            return httpx.Response(200, json=[{"coin": coin, "dailyInterest": "0.0002"}])
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    cfg = settings(
        market_type="margin",
        binance_api_key="k",
        binance_api_secret="s",
        binance_account_rest_url="https://api.binance.com",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceIntelligenceProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert "/sapi/v1/margin/priceIndex" in seen
    assert "/sapi/v1/margin/next-hourly-interest-rate" in seen
    metrics = {row.metric: row.value for row in rows}
    assert metrics["margin_price_index"] == 64000
    assert metrics["borrow_hourly_interest"] == 0.000008
    assert metrics["quote_borrow_hourly_interest"] == 0.00001
    assert metrics["borrow_daily_interest"] == 0.0002


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
        if request.url.path == "/v1/open-interest-history":
            assert request.url.params["symbols"] == "BTCUSDT_PERP.A"
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT_PERP.A", "history": [{"t": 1_759_900_000, "c": 1000}]}],
            )
        assert request.url.path != "/v1/open-interest"
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert provider.mappings()[0]["provider_symbol"] == "BTCUSDT_PERP.A"
    assert any(row.metric == "open_interest_usd" and row.value == 1000 for row in rows)


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
    assert context["derivatives"]["liquidation_intensity"] is None
    assert context["microstructure"]["taker_imbalance_5m"] is None
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


def _market(symbol: str, exchange: str, base: str) -> dict:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "base_asset": base,
        "quote_asset": "USDT",
        "is_perpetual": True,
        "margined": "STABLE",
        "has_long_short_ratio_data": True,
        "has_buy_sell_data": True,
    }


@pytest.mark.asyncio
async def test_coinalyze_asks_one_binance_market_and_keeps_history_inside_the_budget():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/future-markets":
            seen.append((request.url.path, request.url.params["symbols"]))
        if request.url.path == "/v1/future-markets":
            return httpx.Response(
                200,
                json=[
                    _market("BTCUSDT_PERP.B", "B", "BTC"),
                    _market("BTCUSDT_PERP.A", "A", "BTC"),
                    _market("BNBUSDT_PERP.A", "A", "BNB"),
                ],
            )
        if request.url.path == "/v1/funding-rate-history":
            assert request.url.params["interval"] == "1hour"
        if request.url.path == "/v1/ohlcv-history":
            assert request.url.params["interval"] == "5min"
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret", coinalyze_requests_per_minute=40)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        await provider.collect(["BTCUSDT", "BNBUSDT", "XRPUSDT"], _now())
    assert seen
    assert {symbols for _, symbols in seen} == {"BNBUSDT_PERP.A,BTCUSDT_PERP.A"}
    paths = [path for path, _ in seen]
    assert "/v1/funding-rate-history" in paths
    assert "/v1/predicted-funding-rate-history" in paths
    assert "/v1/liquidation-history" in paths
    assert "/v1/ohlcv-history" in paths
    assert "/v1/open-interest" not in paths
    assert 1 + sum(item.count(",") + 1 for _, item in seen) <= 40


@pytest.mark.asyncio
async def test_coinalyze_stops_before_the_minute_budget_is_spent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/future-markets":
            seen.append(request.url.path)
        if request.url.path == "/v1/future-markets":
            return httpx.Response(200, json=[_market("BTCUSDT_PERP.A", "A", "BTC"), _market("ETHUSDT_PERP.A", "A", "ETH")])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret", coinalyze_requests_per_minute=5)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        await provider.collect(["BTCUSDT", "ETHUSDT"], _now())
    assert seen == ["/v1/funding-rate", "/v1/predicted-funding-rate"]
    assert (await provider.health()).last_error == "BUDGET"


def test_history_reaches_jev_without_turning_a_missing_series_into_zero():
    now = _now()
    rows = []
    for index, price in enumerate((100.0, 110.0, 120.0, 130.0, 160.0)):
        rows.append(_obs("open_interest_usd", "SOLUSDT", price, now - timedelta(minutes=20 * (4 - index))))
    for index, rate in enumerate((0.00005, 0.0001, 0.0002, 0.0008)):
        rows.append(_obs("funding_rate", "SOLUSDT", rate, now - timedelta(hours=3 - index)))
    rows.append(_obs("predicted_funding", "SOLUSDT", 0.0002, now))
    for index, (buy, sell) in enumerate(((10.0, 10.0), (30.0, 10.0))):
        stamp = now - timedelta(minutes=5 * (1 - index))
        rows.append(_obs("buy_volume", "SOLUSDT", buy, stamp))
        rows.append(_obs("sell_volume", "SOLUSDT", sell, stamp))
    for index, (long_liq, short_liq) in enumerate(((1.0, 1.0), (1.0, 1.0), (1.0, 1.0), (1.0, 3.0))):
        stamp = now - timedelta(minutes=5 * (3 - index))
        rows.append(_obs("long_liquidations", "SOLUSDT", long_liq, stamp))
        rows.append(_obs("short_liquidations", "SOLUSDT", short_liq, stamp))
    context = build_context("SOLUSDT", {"imbalance_5": 0.2, "taker_flow_1m": 0.1}, rows, now)
    derivatives = context["derivatives"]
    assert derivatives["oi_change_1h"] is not None
    assert derivatives["funding_crowding"] is not None
    assert derivatives["predicted_funding_bps"] == pytest.approx(2.0)
    assert derivatives["predicted_funding_crowding"] is None
    assert derivatives["liquidation_imbalance"] == pytest.approx(0.5, abs=1e-6)
    assert derivatives["liquidation_intensity"] == pytest.approx(1.0)
    flow = context["microstructure"]
    assert flow["taker_imbalance_5m"] == pytest.approx(0.5)
    assert flow["cvd_direction"] is not None
    assert flow["spread_stress"] is None
    empty = build_context("XRPUSDT", {}, rows, now)
    assert empty["microstructure"]["taker_imbalance_5m"] is None
    assert empty["derivatives"]["oi_change_1h"] is None
    assert empty["derivatives"]["funding_crowding"] is None
    assert empty["derivatives"]["liquidation_intensity"] is None


def test_prices_we_already_have_fill_relative_strength_and_dominance():
    now = _now()
    marks = {
        "ETHUSDT": [(now - timedelta(minutes=5), 100.0), (now, 110.0)],
        "BTCUSDT": [(now - timedelta(minutes=5), 100.0), (now, 100.0)],
        "SOLUSDT": [(now - timedelta(minutes=5), 100.0), (now, 90.0)],
    }
    rows = [
        RawExternalObservation(
            provider="alternative_me",
            metric="btc_dominance",
            symbol=None,
            value=0.60,
            unit="fraction",
            observed_at=now - timedelta(minutes=2),
            received_at=now,
        ),
        RawExternalObservation(
            provider="alternative_me",
            metric="btc_dominance",
            symbol=None,
            value=0.65,
            unit="fraction",
            observed_at=now,
            received_at=now,
        ),
    ]
    context = build_context("ETHUSDT", {"spread_bps": 16, "taker_flow_5m": 0.2}, rows, now, marks)
    assert context["relative_strength"]["eth_vs_btc"] is not None
    assert context["relative_strength"]["sol_vs_btc"] is not None
    assert context["global"]["btc_dominance_change"] == pytest.approx(0.05)
    assert context["global"]["alt_rotation_pressure"] < 0
    assert context["microstructure"]["spread_stress"] == 1
    assert context["microstructure"]["taker_imbalance_5m"] == pytest.approx(0.2)
    assert context["microstructure"]["cvd_direction"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_current_funding_is_kept_when_history_is_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/future-markets":
            return httpx.Response(200, json=[_market("BTCUSDT_PERP.A", "A", "BTC")])
        if request.url.path == "/v1/funding-rate":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT_PERP.A", "value": 0.0001, "update": 1_759_900_000}])
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    cfg = settings(coinalyze_enabled=True, coinalyze_api_key="secret")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = CoinalyzeProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert any(row.metric == "funding_rate" and row.value == 0.0001 for row in rows)


@pytest.mark.asyncio
async def test_oregon_reads_margin_context_through_singapore():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.host == "gateway.example"
        assert request.headers["authorization"] == "Bearer share"
        body = json.loads(request.content)
        if body["kind"] == "price_index":
            return httpx.Response(200, json={"status": "ok", "body": {"price": "2500", "calcTime": 1_759_900_000_000}})
        if body["kind"] == "hourly_interest":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "body": [
                        {"asset": "ETH", "nextHourlyInterestRate": "0.00001"},
                        {"asset": "USDT", "nextHourlyInterestRate": "0.00002"},
                    ],
                },
            )
        return httpx.Response(200, json={"status": "ok", "body": [{"coin": body["asset"], "dailyInterest": "0.0002"}]})

    transport = httpx.MockTransport(handler)
    cfg = settings(
        market_type="margin",
        balance_upstream_url="https://gateway.example",
        balance_share_token="share",
        binance_api_key="k",
        binance_api_secret="s",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceIntelligenceProvider(cfg, client)
        rows = await provider.collect(["ETHUSDT"], _now())
    assert seen
    assert set(seen) == {"/api/binance/margin-read"}
    metrics = {row.metric: row.value for row in rows}
    assert metrics["margin_price_index"] == 2500
    assert metrics["borrow_hourly_interest"] == 0.00001
    assert metrics["quote_borrow_hourly_interest"] == 0.00002


def test_margin_read_rejects_anything_except_the_three_account_reads():
    from fastapi.testclient import TestClient

    from app.api.router import create_app
    from tests.conftest import engine

    eng = engine(service_role="account", binance_api_key="k", binance_api_secret="s")
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)
    denied = client.post("/api/binance/margin-read", json={"kind": "withdraw", "symbol": "ETHUSDT"})
    assert denied.status_code == 400
    oregon = engine(service_role="engine", balance_upstream_url="https://gateway.example")
    app.state.engine = oregon
    app.state.settings = oregon.settings
    blocked = client.post("/api/binance/margin-read", json={"kind": "price_index", "symbol": "ETHUSDT"})
    assert blocked.status_code == 409


def _obs(metric: str, symbol: str, value: float, observed: datetime) -> RawExternalObservation:
    return RawExternalObservation(
        provider="coinalyze",
        metric=metric,
        symbol=symbol,
        value=value,
        unit="usd",
        observed_at=observed,
        received_at=observed,
    )
