from unittest.mock import AsyncMock, Mock
import asyncio
from uuid import uuid4
import pytest
from app.domain.enums import Action
from tests.conftest import engine, clock


def position(eng):
    return eng.accounts.open_position(strategy='baseline',symbol='BTCUSDT',side=Action.LONG,
        quantity=1,entry_price=100,stop=99,target=103,opened_at=clock(),decision_id=uuid4(),
        entry_fee=.1,initial_net_risk=1.2,margin=20,mode='live')


def test_account_audit_retains_offsetting_debt_and_stop_identity():
    eng=engine();p=position(eng);p.stop_client_order_id='protective-stop'
    sample=eng.account_sample({'assets':[{'asset':'BTC','total':0,'free':0,
        'locked':'1','borrowed':'1','interest':'0.01'}]},20)
    assert sample['assets'][0]['locked']==1
    assert sample['assets'][0]['borrowed']==1
    assert sample['assets'][0]['interest']==.01
    assert sample['positions'][0]['stop_client_order_id']=='protective-stop'
    assert sample['positions'][0]['decision_id']==str(p.decision_id)


@pytest.mark.asyncio
async def test_recovery_audit_keeps_original_error_and_latest_cause():
    eng=engine();p=position(eng);p.protection_error='original exchange rejection'
    eng.postgres=Mock(healthy=True,save_event=AsyncMock(),save_checkpoint=AsyncMock())
    await eng._exit_blocked('baseline',p,'NO_CLOSABLE_BALANCE_RECONCILE',{'free':0})
    await asyncio.sleep(0)
    kind,reason,payload=eng.postgres.save_event.await_args.args
    assert kind=='live_exit_blocked' and reason=='NO_CLOSABLE_BALANCE_RECONCILE'
    assert payload['protection_error']=='original exchange rejection'
    assert payload['position_id']==str(p.position_id)
    eng.postgres.save_checkpoint.assert_awaited_once()


@pytest.mark.asyncio
async def test_touch_is_recorded_once_with_executable_quote_even_while_management_waits():
    eng=engine();p=position(eng)
    state=eng.states['BTCUSDT'];state.best_bid=103.1;state.best_ask=103.2;state.last_price=103.15
    waiting=asyncio.create_task(asyncio.Event().wait())
    eng._quote_tasks['BTCUSDT']=waiting
    try:
        await eng.on_price('BTCUSDT',103.15,clock())
        await eng.on_price('BTCUSDT',103.15,clock())
        await asyncio.sleep(0)
        events=[e for e in eng.store.events if e['kind']=='exit_touch']
        assert len(events)==1
        assert p.exit_observations['TARGET']['quote']==103.1
        assert events[0]['payload']['bid']==103.1
        assert events[0]['payload']['ask']==103.2
    finally:
        waiting.cancel()
        await asyncio.gather(waiting,return_exceptions=True)
