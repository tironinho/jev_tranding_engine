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

    assert provider.estimate("BTCUSDT").taker == pytest.approx(.001)
    quote = await provider.get_fees("BTCUSDT")

    assert quote.maker == pytest.approx(.0008)
    assert quote.taker == pytest.approx(.001)
    assert quote.source == "binance"
    assert client.urls == ["https://api.binance.com/sapi/v1/asset/tradeFee"]
    assert provider.estimate("BTCUSDT") == quote


@pytest.mark.asyncio
async def test_gateway_fee_read_needs_no_local_binance_credentials():
    import httpx
    from app.config import FeeConfig
    from app.service import TradingEngine
    def handler(request):
        assert request.url == "https://gateway.example/api/binance/fees?symbol=SOLUSDT"
        assert request.headers['authorization'] == 'Bearer share-only'
        assert 'X-MBX-APIKEY' not in request.headers
        return httpx.Response(200, json={'symbol':'SOLUSDT','maker':.0008,'taker':.001,'source':'binance'})
    cfg = settings(market_type='margin',balance_upstream_url='https://gateway.example',balance_share_token='share-only')
    assert isinstance(TradingEngine(cfg).fee_provider, BinanceFeeProvider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BinanceFeeProvider(cfg,FeeConfig(),client)
        quote = await provider.get_fees('SOLUSDT')
    assert quote.taker == .001
    assert quote.source == 'binance_gateway'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'symbol':'SOLUSDT','maker':.00075,'taker':.00075,'source':'config'},
    {'symbol':'BTCUSDT','maker':.001,'taker':.001,'source':'binance'},
    {'symbol':'SOLUSDT','maker':.001,'taker':'nan','source':'binance'},
    {'symbol':'SOLUSDT','maker':-.001,'taker':.001,'source':'binance'},
    {'symbol':'SOLUSDT','maker':False,'taker':False,'source':'binance'},
    {'status':'ERROR'},
])
async def test_gateway_errors_and_unverified_quotes_keep_conservative_fallback(payload):
    from app.config import FeeConfig
    cfg = settings(market_type='margin',balance_upstream_url='https://gateway.example',balance_share_token='share')
    provider = BinanceFeeProvider(cfg,FeeConfig(maker_fee_rate=.00075,taker_fee_rate=.00075),Client(payload))
    quote = await provider.get_fees('SOLUSDT')
    assert quote.source == 'config'
    assert quote.maker == quote.taker == .001
    assert not provider._cache


@pytest.mark.asyncio
async def test_verified_fee_cache_expires_instead_of_preserving_an_old_discount(monkeypatch):
    from app.config import FeeConfig
    stamp = [100.0]
    monkeypatch.setattr('app.providers.fees.time.monotonic',lambda:stamp[0])
    client = Client([{'symbol':'BTCUSDT','makerCommission':'.00075','takerCommission':'.00075'}])
    cfg = settings(market_type='margin',binance_api_key='key',binance_api_secret='secret')
    provider = BinanceFeeProvider(cfg,FeeConfig(),client,ttl_s=300)
    assert (await provider.get_fees('BTCUSDT')).taker == .00075
    stamp[0] += 301
    assert provider.estimate('BTCUSDT').taker == .001
    client.payload = [{'symbol':'BTCUSDT','makerCommission':'.001','takerCommission':'.001'}]
    assert (await provider.get_fees('BTCUSDT')).taker == .001
    assert len(client.urls) == 2


def test_account_fee_gateway_is_authenticated_scoped_and_read_only():
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    from app.api.router import create_app
    from app.risk.engine import FeeQuote
    from tests.conftest import engine
    eng = engine(service_role='account',market_type='margin',environment='production',balance_share_token='share')
    eng.fee_provider.get_fees = AsyncMock(return_value=FeeQuote(maker=.001,taker=.001,source='binance'))
    app = create_app(eng.settings,eng)
    app.state.engine,app.state.settings = eng,eng.settings
    client = TestClient(app)
    assert client.get('/api/binance/fees?symbol=BTCUSDT').status_code == 401
    headers = {'authorization':'Bearer share'}
    assert client.get('/api/binance/fees?symbol=UNSUPPORTED',headers=headers).status_code == 400
    response = client.get('/api/binance/fees?symbol=BTCUSDT',headers=headers)
    assert response.status_code == 200
    assert response.json() == {'symbol':'BTCUSDT','maker':.001,'taker':.001,'source':'binance'}
    eng.settings.service_role = 'engine'
    assert client.get('/api/binance/fees?symbol=BTCUSDT',headers=headers).status_code == 409
    eng.fee_provider.get_fees.assert_awaited_once_with('BTCUSDT')
