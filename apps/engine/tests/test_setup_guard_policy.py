"""Production authorization regressions from the 16/10 trade audit."""
import time
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.domain.enums import Action
from app.providers.jev.schemas import SETUP_MARKET_PROMPT
from app.providers.jev.real import RealJevProvider
from app.risk.engine import FeeQuote
from app.risk.plans import candidate_plans
from app.service import TradingEngine
from app.strategies.runners import BaselineJevStrategy
from tests.conftest import settings, long_snapshot, clock
from tests.test_initial_live_profile import AssessmentProvider, context
from tests.test_context_selection import facts
from tests.test_risk_limits import _context


def short_snapshot(**overrides):
    f=dict(**facts(-1),atr=.1,atr_normalized=.001,range_60m=3,range_60m_frac=.03,
        range_1m=.1,ema_alignment=-1,ema_20_slope=-.001,price_vs_ema20=-.002,
        rsi=36,roc=-.003,orderflow_delta_ratio=-.45,imbalance_10=-.3,
        range_position=.18,breakout=False,breakdown=True,recent_swing_high=100.2,
        support_5m=100.02,support_15m=98.5,resistance_5m=101,
        structure_bar_open_5m=(clock()-timedelta(minutes=5)).isoformat())
    f.update(overrides)
    return long_snapshot(**f)


async def assess(eng,snapshot,p=.8):
    provider=AssessmentProvider(p)
    ctx=context(eng,snapshot,provider);ctx.round_trip_fee=.002
    result=await BaselineJevStrategy().evaluate(snapshot,snapshot.features,ctx)
    return result,provider


@pytest.mark.asyncio
async def test_named_break_below_floor_is_vetoed_in_production():
    eng=TradingEngine(settings());snapshot,_=short_snapshot()
    result,provider=await assess(eng,snapshot,.2)
    assert len(provider.requests)==1
    assert result.action is Action.NO_TRADE
    assert 'JEV_LOW_CONTINUATION' in result.reason_codes
    assert 'size_scale' not in result.metadata


@pytest.mark.asyncio
async def test_valid_short_has_a_real_acceptance_path_with_unchanged_structural_target():
    eng=TradingEngine(settings());snapshot,book=short_snapshot()
    result,provider=await assess(eng,snapshot)
    assert result.action is Action.SHORT
    plan=result.metadata['jev_trade_plan']
    assert plan['target']==98.5
    assert plan['target']==plan['original_target'] and not plan['target_extended_for_costs']
    assert provider.requests[0].prompt_version==SETUP_MARKET_PROMPT
    risk=eng.risk.evaluate(result,snapshot,_context(equity=20,cash=20,day_start_equity=20,live_armed=True),
                           FeeQuote(.001,.001,'test'),book)
    assert risk.accepted,risk.reject_reasons
    assert risk.economics.target==98.5


@pytest.mark.asyncio
async def test_live_long_is_paused_before_any_provider_call():
    eng=TradingEngine(settings());snapshot,_=long_snapshot()
    result,provider=await assess(eng,snapshot)
    assert result.action is Action.NO_TRADE
    assert 'LIVE_LONG_DISABLED' in result.reason_codes and not provider.requests


@pytest.mark.asyncio
async def test_countertrend_break_waits_for_held_reclaim_retest():
    eng=TradingEngine(settings());snapshot,_=short_snapshot(context_15m_slope=.002)
    result,provider=await assess(eng,snapshot)
    assert result.action is Action.NO_TRADE and not provider.requests
    assert 'REVERSAL_RETEST_REQUIRED' in result.reason_codes
    confirmed=snapshot.model_copy(update={'features':{**snapshot.features,'reclaim_retest_short_5m':True}})
    result,provider=await assess(eng,confirmed)
    assert result.action is Action.SHORT and provider.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp',[None,(clock()+timedelta(minutes=5)).isoformat(),
                                (clock()-timedelta(minutes=20)).isoformat()])
async def test_missing_stale_or_forming_structure_never_reaches_provider(stamp):
    eng=TradingEngine(settings());snapshot,_=short_snapshot(structure_bar_open_5m=stamp)
    result,provider=await assess(eng,snapshot)
    assert result.action is Action.NO_TRADE and not provider.requests
    assert any(r.startswith('ENTRY_STRUCTURE_')for r in result.reason_codes)


@pytest.mark.asyncio
async def test_small_range_is_rejected_instead_of_extending_target_for_costs():
    eng=TradingEngine(settings());snapshot,_=short_snapshot(range_60m=.1,range_60m_frac=.001)
    plan=candidate_plans(snapshot,Action.SHORT,eng.risk.limits,.001)[0]
    assert not plan['eligible'] and not plan['target_extended_for_costs']
    assert plan['target']==pytest.approx(snapshot.best_bid-.1)
    result,provider=await assess(eng,snapshot)
    assert result.action is Action.NO_TRADE and not provider.requests
    assert 'STRUCTURAL_TARGET_BELOW_COST_FLOOR' in result.reason_codes


@pytest.mark.asyncio
async def test_risk_backstop_rejects_a_forged_low_probability_break():
    eng=TradingEngine(settings());snapshot,book=short_snapshot()
    result,_=await assess(eng,snapshot)
    result.metadata['jev_continuation']=.2
    risk=eng.risk.evaluate(result,snapshot,_context(),FeeQuote(.001,.001,'test'),book)
    assert not risk.accepted and 'JEV_LOW_CONTINUATION' in risk.reject_reasons


@pytest.mark.asyncio
async def test_risk_cannot_accept_a_farther_target_even_if_extension_flag_is_missing():
    eng=TradingEngine(settings());snapshot,book=short_snapshot()
    result,_=await assess(eng,snapshot)
    result.metadata['jev_trade_plan']['target']=95
    risk=eng.risk.evaluate(result,snapshot,_context(live_armed=True),FeeQuote(.001,.001,'test'),book)
    assert not risk.accepted and 'TARGET_EXCEEDS_OBSERVED_STRUCTURE' in risk.reject_reasons


@pytest.mark.asyncio
async def test_actual_http_body_contains_setup_geometry_and_no_future_results():
    eng=TradingEngine(settings());snapshot,_=short_snapshot()
    bodies=[]
    def handler(request):
        import json
        body=json.loads(request.content);bodies.append(body)
        return httpx.Response(200,json={'answers':{name:{'noul':value}for name,value in
            [('trend_continuation_probability',.8),('reversal_probability',.2),('false_breakout_probability',.2)]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler))as client:
        provider=RealJevProvider(prompt_version='default',base_url='https://test.invalid',
            api_key='test',model='test',client=client)
        ctx=context(eng,snapshot,provider);ctx.round_trip_fee=.002
        result=await BaselineJevStrategy().evaluate(snapshot,snapshot.features,ctx)
    state=bodies[0]['state']
    assert result.prompt_version==SETUP_MARKET_PROMPT
    assert state['setup']['eligible'] and state['setup']['setup']=='CONTINUATION'
    assert state['evidence']['structure']['breakdown_5m'] is True
    assert state['trade']['target_range_60m']<1
    assert state['trade']['time_exit']['rule']=='TARGET_OR_STOP'
    assert state['probability_status']=='UNCALIBRATED'
    assert not any(k in state for k in ['future_returns','realized_pnl','baseline_scores'])


@pytest.mark.asyncio
async def test_pausing_longs_does_not_close_or_modify_an_existing_protected_long(monkeypatch):
    monkeypatch.setattr('app.service.utcnow',clock)
    eng=TradingEngine(settings())
    p=eng.accounts.open_position(strategy='baseline_jev',symbol='BTCUSDT',side=Action.LONG,
        quantity=1,entry_price=100,stop=99,target=103,opened_at=clock()-timedelta(hours=2),
        decision_id=uuid4(),entry_fee=.1,initial_net_risk=1.2,margin=20,mode='live')
    p.stop_client_order_id='existing-stop';p.protection_status='PROTECTED'
    eng._stop_checked[str(p.position_id)]=time.monotonic()
    state=eng.states['BTCUSDT'];state.last_price=101;state.best_bid=101;state.best_ask=101.01
    eng._exit_live=AsyncMock()
    await eng.manage_positions('BTCUSDT',as_of=clock())
    eng._exit_live.assert_not_awaited()
    assert p.stop_client_order_id=='existing-stop' and p.stop==99 and p.target==103
    assert str(p.position_id)in eng.accounts.accounts['baseline_jev'].positions
