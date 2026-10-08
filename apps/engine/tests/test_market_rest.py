import pytest

from app.market.feed import MarketFeed
from tests.conftest import settings


class _Response:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = "" if status == 200 else "blocked"

    def json(self):
        return self._body


class _Client:
    def __init__(self, script: list[tuple[int, object]]):
        self.script = list(script)
        self.calls: list[tuple[str, dict, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        status, body = self.script.pop(0)
        return _Response(status, body)


def _feed(**overrides) -> MarketFeed:
    data = dict(
        market_type="margin",
        symbols="BTCUSDT",
        binance_spot_rest_url="https://data-api.binance.vision",
        binance_api_key="key",
        binance_api_secret="secret",
    )
    data.update(overrides)
    return MarketFeed(settings(**data), {}, None, None, None)


@pytest.mark.asyncio
async def test_public_418_falls_through_to_the_keyed_host():
    client = _Client(
        [
            (418, None),
            (418, None),
            (418, None),
            (200, [[1, "1", "1", "1", "1", "1"]]),
            (200, [[1, "1", "1", "1", "1", "1"]]),
        ]
    )
    feed = _feed()
    await feed._prepare_market_rest(client)
    assert feed.rest_base() == "https://api.binance.com"
    assert feed._rest_mode == "keyed"
    assert feed.breaker.failures == 0
    await feed._get(client, "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 300})
    url, params, headers = client.calls[-1]
    assert url == "https://api.binance.com/api/v3/klines"
    assert "signature" not in params
    assert headers["X-MBX-APIKEY"] == "key"
    assert "python" not in headers["User-Agent"].lower()


@pytest.mark.asyncio
async def test_signed_route_is_used_when_the_key_alone_is_blocked():
    rows = [[1, "1", "1", "1", "1", "1"]]
    client = _Client([(418, None), (200, rows), (200, rows)])
    feed = _feed(binance_spot_rest_url="https://api.binance.com")
    await feed._prepare_market_rest(client)
    assert feed.rest_base() == "https://api.binance.com"
    assert feed._rest_mode == "signed"
    await feed._get(client, "/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 300})
    _, params, headers = client.calls[-1]
    assert "signature" in params
    assert headers["X-MBX-APIKEY"] == "key"


@pytest.mark.asyncio
async def test_open_public_host_stays_unsigned():
    client = _Client([(200, [[1, "1", "1", "1", "1", "1"]])])
    feed = _feed(binance_api_key="", binance_api_secret="")
    await feed._prepare_market_rest(client)
    assert feed.rest_base() == "https://data-api.binance.vision"
    assert feed._rest_mode == "plain"
    assert "X-MBX-APIKEY" not in client.calls[0][2]
