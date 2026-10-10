import pytest

from app.providers.fees import BinanceFeeProvider
from tests.conftest import settings


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    async def get(self, url, **kwargs):
        self.urls.append(url)
        return Response(self.payload)


@pytest.mark.asyncio
async def test_margin_fee_provider_reads_spot_account_trade_fee():
    cfg = settings(market_type="margin", binance_api_key="key", binance_api_secret="secret")
    from app.config import FeeConfig
    client = Client([{"symbol": "BTCUSDT", "makerCommission": "0.0008", "takerCommission": "0.001"}])
    provider = BinanceFeeProvider(cfg, FeeConfig(maker_fee_rate=.00075, taker_fee_rate=.00075), client)

    assert provider.estimate("BTCUSDT").taker == pytest.approx(.00075)
    quote = await provider.get_fees("BTCUSDT")

    assert quote.maker == pytest.approx(.0008)
    assert quote.taker == pytest.approx(.001)
    assert quote.source == "binance"
    assert client.urls == ["https://api.binance.com/sapi/v1/asset/tradeFee"]
    assert provider.estimate("BTCUSDT") == quote
