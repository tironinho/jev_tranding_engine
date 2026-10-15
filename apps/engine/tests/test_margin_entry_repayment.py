"""Entry assets must remain available for protection despite older margin debt."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.router import create_app
from app.execution.binance_live import BinanceExecutionProvider
from tests.conftest import engine, settings
from tests.test_providers import _intent


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['BUY', 'SELL'])
async def test_entry_preserves_received_assets_with_prior_debt_and_close_repays(side):
    # XRP incident: 0.40599 free - 0.46885731 debt = -0.06286731 net.
    # AUTO_BORROW_REPAY consumed the received asset and left only 4.03713269
    # after a 4.1 fill, making a full protective stop impossible.
    received, funding = ('XRP', 'USDT') if side == 'BUY' else ('USDT', 'XRP')
    free = {received: .40599, funding: 0.0}
    debt = {received: .46885731, funding: 0.0}
    locked = {}
    effects = []

    def handler(request):
        params = request.url.params
        if request.method == 'GET':
            return httpx.Response(400, json={'code': -2013, 'msg': 'Order does not exist'})
        if request.method == 'DELETE':
            for asset, amount in locked.items():
                free[asset] += amount
            locked.clear()
            return httpx.Response(200, json={'status': 'CANCELED'})
        effect = params['sideEffectType']
        effects.append(effect)
        quantity = float(params['quantity'])
        sell = params['side'] == 'SELL'
        spent, obtained = ('XRP', 'USDT') if sell else ('USDT', 'XRP')
        cost = quantity * (float(params['price']) if not sell and params['type'] == 'STOP_LOSS_LIMIT' else 1)
        if params['type'] == 'STOP_LOSS_LIMIT':
            assert effect == 'AUTO_REPAY'
            if free[spent] < cost:
                return httpx.Response(400, json={'code': -3041, 'msg': 'Balance is not enough'})
            free[spent] -= cost
            locked[spent] = cost
            return httpx.Response(200, json={'status': 'NEW', 'origQty': str(quantity),
                'executedQty': '0', 'clientOrderId': params['newClientOrderId']})
        missing = max(0, cost - free[spent])
        if missing:
            assert effect in {'MARGIN_BUY', 'AUTO_BORROW_REPAY'}
            debt[spent] += missing
            free[spent] += missing
        free[spent] -= cost
        free[obtained] += quantity
        if effect in {'AUTO_REPAY', 'AUTO_BORROW_REPAY'}:
            repaid = min(free[obtained], debt[obtained])
            debt[obtained] -= repaid
            free[obtained] -= repaid
        return httpx.Response(200, json={'status': 'FILLED', 'executedQty': str(quantity),
            'cummulativeQuoteQty': str(quantity), 'clientOrderId': params['newClientOrderId']})

    candidate = _intent()
    candidate.symbol = 'XRPUSDT'
    candidate.side = side
    candidate.quantity = 4.1
    cfg = settings(market_type='margin', trading_live_enabled=True, allow_real_orders=True,
        binance_api_key='key', binance_api_secret='secret')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BinanceExecutionProvider(cfg, client)
        entry = await provider.submit(candidate)
        assert entry.filled_quantity == pytest.approx(4.1)
        assert free[received] == pytest.approx(4.50599)
        assert debt[received] == pytest.approx(.46885731)
        stop = await provider.submit_stop(candidate, .99 if side == 'BUY' else 1.01, tick=.0001)
        assert stop.status.value == 'NEW'
        assert stop.quantity == pytest.approx(4.1)
        await provider.cancel(candidate.symbol, stop.client_order_id)
        candidate.side = 'SELL' if side == 'BUY' else 'BUY'
        close = await provider.submit_close(candidate)
        assert close.status.value == 'FILLED'
    assert effects == ['MARGIN_BUY', 'AUTO_REPAY', 'AUTO_REPAY']
    assert debt[funding] == pytest.approx(0)


@pytest.mark.parametrize('effect,kind,status', [
    ('MARGIN_BUY', 'MARKET', 200),
    ('AUTO_BORROW_REPAY', 'MARKET', 200),  # Rolling-deploy compatibility.
    ('AUTO_REPAY', 'STOP_LOSS_LIMIT', 200),
    ('MARGIN_BUY', 'STOP_LOSS_LIMIT', 400),
    ('AUTO_BORROW_REPAY', 'STOP_LOSS_LIMIT', 400),
])
def test_account_gateway_allows_borrow_only_entry_but_never_borrowing_stops(effect,kind,status):
    eng = engine(service_role='account', market_type='margin')
    eng.live._signed = AsyncMock(return_value={'status': 'NEW'})
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)
    response = client.post('/api/binance/order', json={'method': 'POST', 'params': {
        'symbol': 'BTCUSDT', 'side': 'BUY', 'type': kind, 'quantity': '0.01',
        'sideEffectType': effect, 'isIsolated': 'FALSE'}})
    assert response.status_code == status
    if status == 200:
        assert eng.live._signed.await_args.args[1]['sideEffectType'] == effect
    else:
        eng.live._signed.assert_not_awaited()
