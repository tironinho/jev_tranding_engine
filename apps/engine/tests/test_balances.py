from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.domain.enums import Action
from app.providers.binance_account import BinanceBalanceProvider
from tests.conftest import engine, settings


class _Boom:
    async def get(self, *args, **kwargs):
        raise AssertionError("balance HTTP was called")


@pytest.mark.asyncio
async def test_balance_without_keys_does_not_call():
    provider = BinanceBalanceProvider(settings(), client=_Boom())
    payload = await provider.snapshot()
    assert payload["status"] == "NO_CREDENTIALS"
    assert payload["wallet"] is None


@pytest.mark.asyncio
async def test_futures_balance_is_the_usdt_wallet():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers["X-MBX-APIKEY"]
        assert "signature" in request.url.params
        assert "withdraw" not in request.url.path
        return httpx.Response(
            200,
            json=[
                {"asset": "USDT", "balance": "1500.5", "availableBalance": "1400", "crossUnPnl": "12.25"},
                {"asset": "BTC", "balance": "0", "availableBalance": "0", "crossUnPnl": "0"},
                {"asset": "BNB", "balance": "0.2", "availableBalance": "0.2", "crossUnPnl": "0"},
            ],
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(binance_api_key="k", binance_api_secret="s", market_type="futures")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceBalanceProvider(cfg, client=client)
        payload = await provider.snapshot()
    assert seen["path"] == "/fapi/v2/balance"
    assert seen["key"] == "k"
    assert payload["wallet"] == 1500.5
    assert payload["available"] == 1400.0
    assert payload["unrealized"] == 12.25
    assert [item["asset"] for item in payload["assets"]] == ["USDT", "BNB"]


@pytest.mark.asyncio
async def test_spot_balance_sums_free_and_locked_usdt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/account"
        return httpx.Response(
            200,
            json={
                "balances": [
                    {"asset": "USDT", "free": "80", "locked": "20"},
                    {"asset": "BTC", "free": "0.01", "locked": "0"},
                    {"asset": "ETH", "free": "0", "locked": "0"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(binance_api_key="k", binance_api_secret="s", market_type="spot")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceBalanceProvider(cfg, client=client)
        payload = await provider.snapshot()
    assert payload["status"] == "ok"
    assert payload["wallet"] == 100
    assert payload["available"] == 80
    assert payload["unrealized"] is None
    assert [item["asset"] for item in payload["assets"]] == ["USDT", "BTC"]


@pytest.mark.asyncio
async def test_margin_balance_reads_the_cross_account_not_the_market_host():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        seen["method"] = request.method
        assert "signature" in request.url.params
        assert "order" not in request.url.path
        return httpx.Response(
            200,
            json={
                "marginLevel": "3.40",
                "userAssets": [
                    {"asset": "USDT", "free": "900", "locked": "50", "borrowed": "0", "interest": "0", "netAsset": "950"},
                    {"asset": "BTC", "free": "0.01", "locked": "0", "borrowed": "0.002", "interest": "0", "netAsset": "0.008"},
                    {"asset": "ETH", "free": "0", "locked": "0", "borrowed": "0", "interest": "0", "netAsset": "0"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(
        binance_api_key="k",
        binance_api_secret="s",
        market_type="margin",
        binance_spot_rest_url="https://data-api.binance.vision",
        binance_account_rest_url="https://api.binance.com",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceBalanceProvider(cfg, client=client)
        payload = await provider.snapshot()
    assert seen["method"] == "GET"
    assert seen["host"] == "api.binance.com"
    assert seen["path"] == "/sapi/v1/margin/account"
    assert payload["status"] == "ok"
    assert payload["wallet"] == 950
    assert payload["available"] == 900
    assert payload["margin_level"] == 3.4
    assert payload["unrealized"] is None
    assert [item["asset"] for item in payload["assets"]] == ["USDT", "BTC"]


@pytest.mark.asyncio
async def test_upstream_balance_reads_the_account_gateway():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gateway.example"
        assert request.url.path == "/api/binance/balance"
        assert request.headers["authorization"] == "Bearer share"
        assert "X-MBX-APIKEY" not in request.headers
        return httpx.Response(
            200,
            json={"status": "ok", "market_type": "margin", "asset": "USDT", "wallet": 12, "available": 10, "unrealized": None, "assets": []},
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(
        balance_upstream_url="https://gateway.example",
        balance_share_token="share",
        binance_api_key="k",
        binance_api_secret="s",
        market_type="margin",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        payload = await BinanceBalanceProvider(cfg, client=client).snapshot()
    assert payload["wallet"] == 12
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_account_role_does_not_open_the_market():
    eng = engine(service_role="account")

    async def boom():
        raise AssertionError("market started")

    eng.feed.start = boom
    eng.intelligence.start = boom
    eng.evolution.start = boom
    await eng.start()
    try:
        assert eng.balance.client is not None
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_balance_http_error_has_no_invented_wallet():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": -2015, "msg": "rejected"})

    transport = httpx.MockTransport(handler)
    cfg = settings(binance_api_key="k", binance_api_secret="s")
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceBalanceProvider(cfg, client=client)
        payload = await provider.snapshot()
    assert payload["status"] == "ERROR"
    assert payload["wallet"] is None
    assert payload["detail"] == "HTTP 401"


@pytest.mark.asyncio
async def test_account_curve_keeps_the_start_and_skips_unmarked_borrow_cash():
    eng = engine()
    await eng.note_balance({"status": "ok", "wallet": 19.79, "assets": [{"asset": "USDT", "total": 19.79}]})
    await eng.note_balance(
        {
            "status": "ok",
            "wallet": 47.23,
            "assets": [
                {"asset": "USDT", "total": 47.23},
                {"asset": "BTC", "total": -0.0002},
            ],
        }
    )
    assert len(eng.balance_points) == 1
    eng.states["BTCUSDT"].last_price = 80_000.0
    await eng.note_balance(
        eng.mark_account(
            {
                "status": "ok",
                "wallet": 47.23,
                "assets": [
                    {"asset": "USDT", "total": 47.23},
                    {"asset": "BTC", "total": -0.0002},
                ],
            }
        )
    )
    track = eng.account_curve()
    marked = 47.23 - 0.0002 * 80_000
    assert track["started"] == pytest.approx(19.79)
    assert track["current"] == pytest.approx(marked)
    assert track["change"] == pytest.approx(marked - 19.79)
    assert [point["equity"] for point in track["points"]] == pytest.approx([19.79, marked])


def test_account_sample_keeps_the_mark_the_coins_and_the_open_position():
    eng = engine()
    eng.states["BTCUSDT"].last_price = 100
    eng.accounts.open_position(
        strategy="baseline_jev",
        symbol="BTCUSDT",
        side=Action.SHORT,
        quantity=0.02,
        entry_price=100,
        stop=103,
        target=97,
        opened_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
        decision_id=uuid4(),
        entry_fee=0.01,
        initial_net_risk=0.06,
        margin=0.4,
        mode="live",
    )
    sample = eng.account_sample(
        {
            "status": "ok",
            "market_type": "margin",
            "wallet": 47.23,
            "available": 19.61,
            "margin_level": 1.74,
            "assets": [
                {"asset": "USDT", "free": 19.61, "total": 47.23},
                {"asset": "BTC", "free": 0.0, "total": -0.02},
                {"asset": "ETH", "free": 0.0, "total": 0.0},
            ],
        },
        45.23,
    )
    assert sample["equity"] == 45.23
    assert sample["available"] == 19.61
    assert sample["margin_level"] == 1.74
    assert sample["marks"] == {"BTCUSDT": 100}
    assert [item["asset"] for item in sample["assets"]] == ["USDT", "BTC"]
    assert sample["positions"][0]["symbol"] == "BTCUSDT"
    assert sample["positions"][0]["side"] == "SHORT"
    assert sample["positions"][0]["mode"] == "live"
    assert "secret" not in sample


def test_paper_book_and_open_trade_money():
    eng = engine()
    eng.states["BTCUSDT"].last_price = 105
    eng.accounts.open_position(
        strategy="baseline",
        symbol="BTCUSDT",
        side=Action.LONG,
        quantity=2,
        entry_price=100,
        stop=95,
        target=110,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decision_id=uuid4(),
        entry_fee=1,
        initial_net_risk=10,
        margin=100,
    )
    row = eng.positions_payload()[0]
    assert row["notional"] == 210
    assert row["leverage"] == 2.1
    assert row["unrealized"] == 10 - 105 * 2 * 0.0005
    assert row["target_pnl"] == 20 - 1 - 110 * 2 * 0.0005
    assert row["stop_pnl"] == -10 - 1 - 95 * 2 * 0.0005
    book = eng.paper_book()
    baseline = book["accounts"][0]
    assert baseline["cash"] == 9899
    assert baseline["equity"] == 10009
    assert baseline["unrealized"] == 10
    assert baseline["net_pnl"] == 9
    jev = next(item for item in book["accounts"] if item["strategy"] == "baseline_jev")
    assert jev["net_pnl"] == 0
    assert book["leverage"] == 5
    assert jev["equity"] == 10_000
