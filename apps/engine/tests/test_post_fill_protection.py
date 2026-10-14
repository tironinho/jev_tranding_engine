import asyncio
from unittest.mock import AsyncMock

import pytest

from app.domain.enums import OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from app.execution.binance_live import LiveExecutionBlocked
from app.execution.paper import OrderIntent
from app.service import _free_asset
from tests.conftest import attach_book, clock, engine, long_snapshot
from tests.test_runtime import _Exchange, _arm_live


def stop_order(intent, _price, **kwargs):
    return OrderRecord(client_order_id=intent.decision_id.hex[:31]+kwargs.get('suffix','S'),
        decision_id=intent.decision_id,risk_id=intent.risk_id,strategy=intent.strategy,
        symbol=intent.symbol,side='SELL',order_type=OrderType.STOP_LOSS_LIMIT,
        status=OrderStatus.NEW,mode='live',quantity=intent.quantity,created_at=clock())


def intent():
    from uuid import uuid4
    return OrderIntent(decision_id=uuid4(),risk_id=None,strategy='baseline',symbol='BTCUSDT',
        side='BUY',order_type=OrderType.MARKET,quantity=.999,limit_price=None,mode='live',
        created_at=clock(),best_bid=99.99,best_ask=100.01,book={},fee_rate=.001)


@pytest.mark.asyncio
async def test_entry_protects_net_fill_without_waiting_for_post_fill_account_snapshot(monkeypatch):
    monkeypatch.setattr('app.service.utcnow',clock)
    eng = engine(market_type='margin',trading_live_enabled=True,allow_real_orders=True,
                 binance_api_key='key',binance_api_secret='secret')
    _arm_live(eng)
    snapshot,book = long_snapshot()
    attach_book(eng,book)
    eng.live.client = _Exchange()
    # The first read is the pre-entry risk check. A subsequent zero snapshot
    # reproduces the production incident and must not veto the protective POST.
    eng.balance.snapshot = AsyncMock(side_effect=[
        {'status':'ok','equity_usdt':10000,'wallet':10000,'margin_level':999,
         'assets':[{'asset':'USDT','total':10000}]},
        {'status':'ok','assets':[{'asset':'BTC','free':0,'total':0}]},
    ])
    def filled(candidate):
        return OrderRecord(client_order_id=candidate.decision_id.hex,decision_id=candidate.decision_id,
            risk_id=candidate.risk_id,strategy=candidate.strategy,symbol=candidate.symbol,
            side='BUY',order_type=OrderType.MARKET,status=OrderStatus.FILLED,mode='live',
            quantity=1,filled_quantity=1,average_fill_price=100,created_at=clock(),commissions={'BTC':.001})
    eng.live.submit = AsyncMock(side_effect=filled)
    eng.live.submit_stop = AsyncMock(side_effect=stop_order)
    eng.live.submit_close = AsyncMock()
    await eng.evaluate_snapshot(snapshot)
    position = eng.accounts.accounts['baseline'].sole('BTCUSDT')
    assert position.protection_status == 'PROTECTED'
    assert position.quantity == pytest.approx(.999)
    assert eng.live.submit_stop.await_args.args[0].quantity == pytest.approx(.999)
    assert eng.balance.snapshot.await_count == 1
    eng.live.submit_close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [
    'HTTP 409 {"detail":"-2010 Account has insufficient balance for requested action."}',
    'HTTP 400 {"code":-3041,"msg":"Balance is not enough"}',
])
async def test_definitive_balance_rejection_waits_briefly_for_full_owned_quantity(error):
    eng = engine(market_type='margin')
    eng.feed.rules['BTCUSDT'] = {'step_size':.001}
    candidate = intent()
    eng.live.submit_stop = AsyncMock(side_effect=[LiveExecutionBlocked(error),stop_order(candidate,99)])
    eng.balance.snapshot = AsyncMock(side_effect=[
        {'status':'ok','observed_at':1,'assets':[{'asset':'BTC','free':0}]},
        {'status':'ok','observed_at':2,'assets':[{'asset':'BTC','free':.001}]},
        {'status':'ok','observed_at':3,'assets':[{'asset':'BTC','free':'0.999'}]},
    ])
    result = await eng._submit_protective_stop(candidate,99)
    assert result.status is OrderStatus.NEW
    assert eng.live.submit_stop.await_count == 2
    assert eng.balance.snapshot.await_count == 3
    assert candidate.quantity == .999
    assert eng.live.submit_stop.await_args_list[0].kwargs == eng.live.submit_stop.await_args_list[1].kwargs
    logs = [r for r in eng.store.events if r['kind']=='live_stop_balance']
    assert len(logs) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [TimeoutError('unknown result'),
    LiveExecutionBlocked('HTTP 502 gateway unavailable'),LiveExecutionBlocked('HTTP 400 -1013 NOTIONAL')])
async def test_unknown_or_non_balance_stop_failure_is_never_blindly_retried(error):
    eng = engine(market_type='margin')
    eng.live.submit_stop = AsyncMock(side_effect=error)
    eng.balance.snapshot = AsyncMock()
    with pytest.raises(type(error)):
        await eng._submit_protective_stop(intent(),99)
    eng.live.submit_stop.assert_awaited_once()
    eng.balance.snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_balance_has_a_total_confirmation_deadline():
    eng = engine(market_type='margin')
    eng.live.submit_stop = AsyncMock(side_effect=LiveExecutionBlocked('-3041 Balance is not enough'))
    async def stalled(**kwargs):
        await asyncio.Event().wait()
    eng.balance.snapshot = stalled
    with pytest.raises(LiveExecutionBlocked,match='NO_PROTECTABLE_QUANTITY_AFTER_BALANCE_CONFIRMATION'):
        await asyncio.wait_for(eng._submit_protective_stop(intent(),99),timeout=3)
    eng.live.submit_stop.assert_awaited_once()


@pytest.mark.parametrize('free,expected', [('0.999',.999),(0,0),('nan',None),('inf',None),
    (-1,None),(True,None),(None,None),('invalid',None)])
def test_free_base_normalizes_strings_but_does_not_turn_errors_into_balances(free,expected):
    assert _free_asset({'status':'ok','assets':[{'asset':'BTC','free':free}]},'BTC') == expected
    assert _free_asset({'status':'ERROR','assets':[{'asset':'BTC','free':1}]},'BTC') is None
