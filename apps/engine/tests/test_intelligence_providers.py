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
            return httpx.Response(200, json=[{"symbol": "BTCUSDT_PERP.A", "value": 0.01, "update": 1_759_900_000}])
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


@pytest.mark.asyncio
async def test_cryptoquant_reads_only_the_paths_its_catalog_lists():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer cq"
        if request.url.path == "/v1/discovery/endpoints":
            return httpx.Response(
                200,
                json={"result": ["/btc/exchange-flows/netflow", "/stablecoin/network-data/supply"]},
            )
        if request.url.path == "/v1/btc/exchange-flows/netflow":
            assert request.url.params["exchange"] == "all_exchange"
            return httpx.Response(
                200,
                json={
                    "status": {"code": 200, "message": "success"},
                    "result": {
                        "window": "day",
                        "data": [
                            {"date": "2026-10-06", "netflow_total": -100},
                            {"date": "2026-10-07", "netflow_total": -80},
                            {"date": "2026-10-08", "netflow_total": -40},
                        ],
                    },
                },
            )
        if request.url.path == "/v1/stablecoin/network-data/supply":
            return httpx.Response(
                200,
                json={
                    "status": {"code": 200},
                    "result": {
                        "data": [
                            {"date": "2026-10-06", "supply_circulating": 100},
                            {"date": "2026-10-07", "supply_circulating": 105},
                            {"date": "2026-10-08", "supply_circulating": 110},
                        ]
                    },
                },
            )
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    cfg = settings(cryptoquant_enabled=True, cryptoquant_api_key="cq", cryptoquant_base_host="api.cryptoquant.com")
    async with httpx.AsyncClient(transport=transport) as client:
        from app.intelligence.providers.optional import CryptoQuantProvider

        provider = CryptoQuantProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert "/v1/stablecoin/exchange-flows/reserve" not in seen
    metrics = {row.metric: row.value for row in rows}
    assert metrics["exchange_netflow"] == -40
    assert metrics["stablecoin_supply"] == 110
    context = build_context("ETHUSDT", {}, rows, _now())
    assert context["onchain"]["exchange_netflow_btc"] == -40
    assert context["onchain"]["stablecoin_reserve"] == 110
    assert context["onchain"]["available"] is True
    assert context["onchain"]["exchange_flow_pressure"] is not None


@pytest.mark.asyncio
async def test_cryptoquant_does_not_guess_a_path_missing_from_the_catalog():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"result": ["/btc/market-data/price-usd"]})

    transport = httpx.MockTransport(handler)
    cfg = settings(cryptoquant_enabled=True, cryptoquant_api_key="cq")
    async with httpx.AsyncClient(transport=transport) as client:
        from app.intelligence.providers.optional import CryptoQuantProvider

        provider = CryptoQuantProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert rows == []
    assert seen == ["/v1/discovery/endpoints"]
    assert (await provider.health()).last_error == "CATALOG_MISSING"


@pytest.mark.asyncio
async def test_coinmetrics_community_reads_exchange_flow_without_a_key():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert "api_key" not in request.url.params
        if request.url.path == "/v4/catalog-v2/asset-metrics":
            return httpx.Response(200, json=_cm_catalog())
        if request.url.path == "/v4/timeseries/asset-metrics" and request.url.params["assets"] == "btc":
            assert request.url.params["metrics"] == "FlowInExUSD,FlowOutExUSD"
            assert "ReferenceRateUSD" not in request.url.params["metrics"]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"asset": "btc", "time": "2026-10-06T00:00:00.000000000Z", "FlowInExUSD": "300", "FlowOutExUSD": "100"},
                        {"asset": "btc", "time": "2026-10-07T00:00:00.000000000Z", "FlowInExUSD": "250", "FlowOutExUSD": "150"},
                        {"asset": "btc", "time": "2026-10-08T00:00:00.000000000Z", "FlowInExUSD": "220", "FlowOutExUSD": "180"},
                        {"asset": "btc", "time": "2026-10-09T00:00:00.000000000Z", "FlowInExUSD": None, "FlowOutExUSD": None},
                    ]
                },
            )
        if request.url.path == "/v4/timeseries/asset-metrics":
            assert request.url.params["assets"] == "usdc,usdt"
            assert request.url.params["metrics"] == "SplyCur"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"asset": "usdt", "time": "2026-10-06T00:00:00.000000000Z", "SplyCur": "100"},
                        {"asset": "usdc", "time": "2026-10-06T00:00:00.000000000Z", "SplyCur": "40"},
                        {"asset": "usdt", "time": "2026-10-07T00:00:00.000000000Z", "SplyCur": "110"},
                        {"asset": "usdc", "time": "2026-10-07T00:00:00.000000000Z", "SplyCur": "40"},
                        {"asset": "usdt", "time": "2026-10-08T00:00:00.000000000Z", "SplyCur": "120"},
                        {"asset": "usdc", "time": "2026-10-08T00:00:00.000000000Z", "SplyCur": "50"},
                        {"asset": "usdt", "time": "2026-10-09T00:00:00.000000000Z", "SplyCur": "130"},
                    ]
                },
            )
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    cfg = settings(coinmetrics_enabled=False, coinmetrics_api_key="", coinmetrics_community=True, coinmetrics_base_host="community-api.coinmetrics.io")
    async with httpx.AsyncClient(transport=transport) as client:
        from app.intelligence.providers.optional import CoinMetricsProvider

        provider = CoinMetricsProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
        again = await provider.collect(["BTCUSDT"], _now())
    assert again == []
    assert seen.count("/v4/timeseries/asset-metrics") == 2
    metrics = {row.metric: row.value for row in rows}
    assert metrics["exchange_netflow"] == pytest.approx(40)
    assert metrics["stablecoin_supply"] == pytest.approx(170)
    context = build_context("ETHUSDT", {}, rows, _now())
    assert context["onchain"]["exchange_netflow_btc"] == pytest.approx(40)
    assert context["onchain"]["stablecoin_reserve"] == pytest.approx(170)
    assert context["onchain"]["available"] is True
    assert context["onchain"]["source"] == "coinmetrics"
    assert context["onchain"]["exchange_flow_pressure"] is not None


@pytest.mark.asyncio
async def test_coinmetrics_skips_a_metric_the_community_catalog_does_not_list():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"data": [{"asset": "btc", "metrics": [{"metric": "ReferenceRateUSD", "frequencies": [{"frequency": "1d", "community": False}]}]}]},
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(coinmetrics_community=True, coinmetrics_api_key="")
    async with httpx.AsyncClient(transport=transport) as client:
        from app.intelligence.providers.optional import CoinMetricsProvider

        provider = CoinMetricsProvider(cfg, client)
        rows = await provider.collect(["BTCUSDT"], _now())
    assert rows == []
    assert seen == ["/v4/catalog-v2/asset-metrics"]
    assert (await provider.health()).last_error == "CATALOG_MISSING"


@pytest.mark.asyncio
async def test_coinmetrics_reads_eth_flow_supply_and_mvrv_in_one_call():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4/catalog-v2/asset-metrics":
            return httpx.Response(200, json=_cm_catalog(chain=True))
        if request.url.path != "/v4/timeseries/asset-metrics":
            raise AssertionError(request.url.path)
        assets = request.url.params["assets"]
        metrics = request.url.params["metrics"]
        assert "ReferenceRateUSD" not in metrics
        assert "PriceUSD" not in metrics
        if assets == "btc,eth,xrp":
            assert "CapMVRVCur" in metrics
            assert "FlowInExUSD" in metrics
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"asset": "btc", "time": "2026-10-06T00:00:00.000000000Z", "FlowInExUSD": "30", "FlowOutExUSD": "10", "SplyExUSD": "5", "CapMVRVCur": "1.5", "AdrActCnt": "10"},
                        {"asset": "btc", "time": "2026-10-07T00:00:00.000000000Z", "FlowInExUSD": "20", "FlowOutExUSD": "10", "SplyExUSD": "6", "CapMVRVCur": "1.8", "AdrActCnt": "12"},
                        {"asset": "btc", "time": "2026-10-08T00:00:00.000000000Z", "FlowInExUSD": "50", "FlowOutExUSD": "10", "SplyExUSD": "9", "CapMVRVCur": "3.0", "AdrActCnt": "30"},
                        {"asset": "eth", "time": "2026-10-06T00:00:00.000000000Z", "FlowInExUSD": "8", "FlowOutExUSD": "4", "CapMVRVCur": "1.0"},
                        {"asset": "eth", "time": "2026-10-07T00:00:00.000000000Z", "FlowInExUSD": "8", "FlowOutExUSD": "5", "CapMVRVCur": "1.2"},
                        {"asset": "eth", "time": "2026-10-08T00:00:00.000000000Z", "FlowInExUSD": "20", "FlowOutExUSD": "4", "CapMVRVCur": "2.4"},
                        {"asset": "xrp", "time": "2026-10-08T00:00:00.000000000Z", "CapMVRVCur": "1.1"},
                    ]
                },
            )
        assert assets == "usdc,usdt"
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    cfg = settings(coinmetrics_community=True, coinmetrics_api_key="", coinmetrics_base_host="community-api.coinmetrics.io")
    async with httpx.AsyncClient(transport=transport) as client:
        from app.intelligence.providers.optional import CoinMetricsProvider

        provider = CoinMetricsProvider(cfg, client)
        rows = await provider.collect(["ETHUSDT"], _now())
    symbols = {(row.metric, row.symbol): row.value for row in rows}
    assert symbols["exchange_netflow", "ETHUSDT"] == pytest.approx(16)
    assert symbols["exchange_supply", "BTCUSDT"] == pytest.approx(9)
    assert symbols["mvrv", "XRPUSDT"] == pytest.approx(1.1)
    assert symbols["active_addresses", "BTCUSDT"] == pytest.approx(30)
    assert ("exchange_netflow", "XRPUSDT") not in symbols


def _cm_catalog(chain: bool = False) -> dict:
    def metric(name: str, community: bool = True) -> dict:
        return {"metric": name, "frequencies": [{"frequency": "1d", "community": community}]}

    if not chain:
        return {
            "data": [
                {"asset": "btc", "metrics": [metric("FlowInExUSD"), metric("FlowOutExUSD"), metric("ReferenceRateUSD", False)]},
                {"asset": "usdt", "metrics": [metric("SplyCur")]},
                {"asset": "usdc", "metrics": [metric("SplyCur")]},
            ]
        }
    return {
        "data": [
            {"asset": "btc", "metrics": [metric(name) for name in ("FlowInExUSD", "FlowOutExUSD", "SplyExUSD", "CapMVRVCur", "AdrActCnt")]},
            {"asset": "eth", "metrics": [metric(name) for name in ("FlowInExUSD", "FlowOutExUSD", "CapMVRVCur")]},
            {"asset": "xrp", "metrics": [metric("CapMVRVCur")]},
            {"asset": "usdt", "metrics": [metric("SplyCur")]},
            {"asset": "usdc", "metrics": [metric("SplyCur")]},
        ]
    }


def test_onchain_support_is_oriented_for_the_jev_veto():
    now = _now()

    def point(metric: str, symbol: str | None, value: float, day: int) -> RawExternalObservation:
        return RawExternalObservation(
            provider="coinmetrics",
            metric=metric,
            symbol=symbol,
            value=value,
            unit="usd",
            observed_at=now - timedelta(days=day),
            received_at=now,
        )

    rows = []
    for day, flow, supply, mvrv, addresses, stables in (
        (2, 0.0, 5.0, 1.0, 10.0, 100.0),
        (1, 20.0, 6.0, 1.4, 12.0, 101.0),
        (0, 100.0, 9.0, 3.0, 40.0, 102.0),
    ):
        rows.append(point("exchange_netflow", "ETHUSDT", flow, day))
        rows.append(point("exchange_supply", "ETHUSDT", supply, day))
        rows.append(point("mvrv", "ETHUSDT", mvrv, day))
        rows.append(point("active_addresses", "ETHUSDT", addresses, day))
        rows.append(point("stablecoin_supply", None, stables, day))
    context = build_context("ETHUSDT", {}, rows, now)
    onchain = context["onchain"]
    assert onchain["exchange_flow"] < -0.5
    assert onchain["valuation_stretch"] > 0.5
    assert onchain["support_long"] < 0
    assert onchain["evidence"] == pytest.approx(1.0)
    from app.providers.jev.real import _normalized_state

    long_state = _normalized_state({"symbol": "ETHUSDT", "features": {}, "baseline_action": "LONG", "intelligence": context})
    short_state = _normalized_state({"symbol": "ETHUSDT", "features": {}, "baseline_action": "SHORT", "intelligence": context})
    assert long_state["intelligence"]["onchain"]["class"] == "HOSTILE"
    assert long_state["intelligence"]["onchain"]["support"] < 0
    assert short_state["intelligence"]["onchain"]["class"] == "SUPPORTIVE"
    assert short_state["intelligence"]["onchain"]["support"] > 0
    assert "exchange_netflow_btc" not in long_state["intelligence"]["onchain"]
    assert "stablecoin_reserve" not in long_state["intelligence"]["onchain"]
    assert long_state["intelligence"]["onchain"]["weights"]["exchange_flow"] == pytest.approx(0.35)


def test_thin_onchain_evidence_does_not_label_the_side():
    from app.intelligence.context import _onchain_view

    raw = {
        "support_long": -0.57,
        "evidence": 0.2,
        "stablecoin_liquidity": -0.57,
        "exchange_flow": None,
    }
    view = _onchain_view(raw, "LONG")
    assert view["class"] == "UNKNOWN"
    assert view["support"] is None
    assert view["evidence"] == 0.2
    labeled = _onchain_view({**raw, "evidence": 1.0}, "LONG")
    assert labeled["class"] == "HOSTILE"
    assert labeled["support"] < 0


def test_stale_onchain_does_not_vote():
    now = _now()
    old = datetime(2019, 4, 22, tzinfo=timezone.utc)
    rows = [
        RawExternalObservation(
            provider="coinmetrics",
            metric="mvrv",
            symbol="BNBUSDT",
            value=value,
            unit="ratio",
            observed_at=old - timedelta(days=day),
            received_at=now,
        )
        for day, value in ((2, 1.0), (1, 1.2), (0, 4.0))
    ]
    context = build_context("BNBUSDT", {}, rows, now)
    assert context["onchain"]["support_long"] is None
    assert context["onchain"]["valuation_stretch"] is None


def test_cryptoquant_onchain_wins_when_coinmetrics_is_also_present():
    now = _now()

    def row(provider: str, metric: str, value: float, day: int) -> RawExternalObservation:
        return RawExternalObservation(
            provider=provider,
            metric=metric,
            symbol=None,
            value=value,
            unit="usd",
            observed_at=now - timedelta(days=day),
            received_at=now,
        )

    rows = [
        row("cryptoquant", "exchange_netflow", -40, 0),
        row("cryptoquant", "exchange_netflow", -80, 1),
        row("coinmetrics", "exchange_netflow", 999, 0),
        row("coinmetrics", "stablecoin_supply", 10, 0),
        row("coinmetrics", "stablecoin_supply", 9, 1),
    ]
    context = build_context("ETHUSDT", {}, rows, now)
    assert context["onchain"]["exchange_netflow_btc"] == -40
    assert context["onchain"]["stablecoin_reserve"] == 10
    assert context["onchain"]["source"] == "cryptoquant+coinmetrics"


def test_current_funding_stamp_in_milliseconds_stays_readable():
    from app.intelligence.providers.coinalyze import _current, _seconds

    stamp = _seconds(1_759_900_000_000)
    assert stamp is not None
    assert stamp == datetime.fromtimestamp(1_759_900_000, tz=timezone.utc)
    rows = _current(
        [{"symbol": "BTCUSDT_PERP.A", "value": 0.01, "update": 1_759_900_000_000}],
        [{"provider_symbol": "BTCUSDT_PERP.A", "internal_symbol": "BTCUSDT"}],
        _now(),
        "funding_rate",
    )
    assert rows[0].value == pytest.approx(0.0001)
    assert rows[0].observed_at == stamp


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
