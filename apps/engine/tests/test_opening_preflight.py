import time
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from app.domain.enums import Action,OperatingMode
from app.config import RiskLimits
from app.providers.binance_account import BinanceBalanceProvider
from app.strategies.order_preflight import order_preflight
from app.strategies.role_baseline import assess_roles
from tests.test_role_baseline import viable,_bar
from app.features.engine import five_minute_structure
from tests.conftest import settings,long_snapshot
from tests.test_simple_jev_input import candidate,evaluate
from tests.conftest import historical_simple_engine as TradingEngine


def test_pullback_is_a_separate_shadow_setup_without_a_new_break():
    f=viable(breakout_5m=False,pullback_resume_long_5m=True,taker_flow_5m=-.05)
    result=assess_roles(Action.LONG,f)
    assert result["setup"]=="CONTINUATION_PULLBACK"
    assert result["entry_variants"]["continuation_pullback"]["eligible"]
    assert result["entry_variants"]["continuation_pullback"]["flow_divergent"]
    assert result["can_authorize"] is False
    f["context_15m_slope"]=-.003
    result=assess_roles(Action.LONG,f)
    assert not result["entry_variants"]["continuation_pullback"]["eligible"]
    assert result["context_slope_against"] and result["context_conflicted"]


def test_closed_bar_pullback_is_distinct_from_one_minute_noise():
    bars=[_bar(i,100) for i in range(20)]+[_bar(20,101),_bar(21,100.5),_bar(22,100.8)]
    bars[-1].open=100.6
    f=five_minute_structure(bars,1)
    assert f["pullback_resume_long_5m"] and not f["breakout_5m"]
    assert not f["pullback_resume_short_5m"]


def test_preflight_unknown_data_is_not_a_refusal_and_cache_does_not_fetch():
    assert order_preflight({},Action.LONG,RiskLimits(),{},None,.001)["status"]=="UNKNOWN"
    provider=BinanceBalanceProvider(settings())
    provider._fetch=AsyncMock()
    assert provider.cached_snapshot() is None
    provider._cached=dict(status="ok",observed_at=time.time());provider._cached_at=time.monotonic()
    assert provider.cached_snapshot()["status"]=="ok"
    provider._cached_at-=10
    assert provider.cached_snapshot() is None
    provider._fetch.assert_not_called()


@pytest.mark.parametrize("equity,reason",[(10,"PREFLIGHT_ENTRY_BELOW_MIN_NOTIONAL"),(20,None),(100,None)])
def test_rounding_and_exit_floor_are_checked_under_same_budget(equity,reason):
    plan=dict(entry=100,stop=101,target=80)
    limits=RiskLimits(max_trade_notional_fraction=.30,max_portfolio_notional_fraction=.80)
    result=order_preflight(plan,Action.SHORT,limits,dict(step_size=.01,min_notional=5),equity,.001)
    assert result["reason"]==reason
    assert not result["can_authorize"]


@pytest.mark.asyncio
async def test_infeasible_live_candidate_never_calls_jev_but_paper_keeps_research():
    from tests.test_initial_live_profile import context,AssessmentProvider
    from app.strategies.runners import BaselineJevStrategy
    from app.config import BaselineWeightConfig
    eng=TradingEngine(settings());snapshot,book=candidate()
    eng.weights=replace(eng.weights,version="baseline_first_ten_v2")
    eng.risk.update_limits(max_risk_per_trade=1e-6)
    provider=AssessmentProvider(.9);ctx=context(eng,snapshot,provider)
    ctx.mode=OperatingMode.LIVE;ctx.preflight_equity=10;ctx.exchange_rules=dict(step_size=.001,min_notional=5)
    decision=await BaselineJevStrategy().evaluate(snapshot,snapshot.features,ctx)
    assert decision.action is Action.NO_TRADE
    assert decision.reason_codes==["PREFLIGHT_MINIMUM_EXCEEDS_STOP_RISK"]
    assert not provider.requests
    ctx.mode=OperatingMode.PAPER
    await BaselineJevStrategy().evaluate(snapshot,snapshot.features,ctx)
    assert provider.requests


def test_missing_flow_cannot_qualify_a_pullback_and_warning_preserves_conflict():
    result=assess_roles(Action.LONG,viable(breakout_5m=False,pullback_resume_long_5m=True,taker_flow_5m=None))
    assert not result["entry_variants"]["continuation_pullback"]["eligible"]
    result=assess_roles(Action.LONG,viable(context_15m_slope=-.003))
    assert result["setup"]=="CONTINUATION_WITH_CONFLICT"
    assert result["context_slope_against"]
    assert "CONTEXT_SLOPE_AGAINST" in result["context_warnings"]


def test_stop_risk_still_refuses_a_minimum_the_trade_budget_could_reach():
    limits=RiskLimits(max_trade_notional_fraction=.3, max_portfolio_notional_fraction=.8,
                      max_symbol_exposure=1, max_leverage=5, max_risk_per_trade=.0005)
    result=order_preflight(dict(entry=100, stop=110, target=80), Action.SHORT, limits,
                           dict(step_size=.01, min_notional=5), 100, .001)
    assert result["reason"]=="PREFLIGHT_MINIMUM_EXCEEDS_STOP_RISK"
    assert not result["can_authorize"]


def test_preflight_never_rewrites_limits_or_proposed_plan():
    limits=RiskLimits(max_trade_notional_fraction=.3,max_portfolio_notional_fraction=.8,min_net_rr=2)
    plan=dict(entry=100,stop=101,target=98)
    before=dict(plan)
    order_preflight(plan,Action.SHORT,limits,dict(step_size=.001,min_notional=5),20,.001)
    assert plan==before
    assert limits.max_trade_notional_fraction==.3 and limits.min_net_rr==2


@pytest.mark.parametrize("equity,possible", [(16, True), (12, False)])
def test_preflight_respects_the_bounded_minimum_allocation(equity, possible):
    limits = RiskLimits(max_trade_notional_fraction=.3, max_portfolio_notional_fraction=.8)
    result = order_preflight(dict(entry=100, stop=100.5, target=98), Action.SHORT, limits,
                             dict(step_size=.001, min_notional=5), equity, .001)
    assert (result["status"] == "NOTIONAL_POSSIBLE") is possible
    assert result["optimistic_notional_ceiling"] <= equity * limits.max_leverage * .40 * 1.01 + 1e-9
    assert not result["can_authorize"]
