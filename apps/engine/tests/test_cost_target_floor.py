import json
from dataclasses import replace

import httpx
import pytest

from app.domain.enums import Action, MarketType
from app.providers.jev.real import RealJevProvider
from app.risk.economics import compute_trade_economics, target_for_net_rr
from app.risk.engine import FeeQuote
from app.risk.plans import candidate_plans
from app.strategies.runners import BaselineJevStrategy
from tests.conftest import market_range_engine, long_snapshot, attach_book
from tests.test_initial_live_profile import AssessmentProvider, context
from tests.test_risk_limits import _context


@pytest.mark.parametrize("side", [Action.LONG, Action.SHORT])
@pytest.mark.parametrize("funding", [0, -.01])
def test_target_covers_real_target_commission_slippage_and_borrow(side, funding):
    sign = 1 if side is Action.LONG else -1
    stop = 100-sign*.25
    target = target_for_net_rr(side=side, entry=100, stop=stop, target=100+sign*.5,
        min_net_rr=1.5, entry_fee_rate=.00075, exit_fee_rate=.00075,
        exit_slippage_per_unit=.02, funding_per_unit=funding)
    econ = compute_trade_economics(side=side, entry=100, stop=stop, target=target,
        quantity=2, entry_fee_rate=.00075, exit_fee_rate=.00075,
        exit_slippage_per_unit=.02, funding_cashflow_total=funding*2, fee_source="test")
    assert econ.net_rr == pytest.approx(1.5)
    assert abs(target-100) > .5
    assert target_for_net_rr(side=side, entry=100, stop=stop, target=100+sign*2,
        min_net_rr=1.5, entry_fee_rate=.00075, exit_fee_rate=.00075,
        exit_slippage_per_unit=.02, funding_per_unit=funding) == 100+sign*2


def narrow_snapshot(side=Action.LONG):
    features = dict(atr=.01, atr_normalized=.0001, range_60m=.5, range_60m_frac=.005,
                    recent_swing_low=99.9, recent_swing_high=100.1)
    if side is Action.SHORT:
        features.update(ema_alignment=-1, ema_20_slope=-.001, price_vs_ema20=-.002,
                        rsi=36, roc=-.003, orderflow_delta_ratio=-.45,
                        imbalance_10=-.3, range_position=.18, breakout=False, breakdown=True)
    return long_snapshot(**features)


def cost_target_engine(**overrides):
    eng = market_range_engine(**overrides)
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    return eng


@pytest.mark.parametrize("side", [Action.LONG, Action.SHORT])
@pytest.mark.asyncio
async def test_confirmed_narrow_range_opens_paper_using_the_exact_assessed_target(side):
    eng = cost_target_engine(initial_paper_equity=20)
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    assert eng.risk.limits.cost_aware_target_floor
    snapshot, book = narrow_snapshot(side)
    attach_book(eng, book)
    provider = AssessmentProvider(.65)
    eng.jev = provider
    await eng.evaluate_snapshot(snapshot)
    position = next(iter(eng.accounts.accounts["baseline_jev"].positions.values()))
    plan = provider.requests[0].trade_plan
    assert position.mode == "paper"
    assert position.target == plan["target"]
    assert position.stop == plan["stop"]
    assert plan["target_extended_for_costs"]
    assert plan["net_rr"] == pytest.approx(1.5)
    assert abs(position.target-position.entry_price) > .5


@pytest.mark.asyncio
async def test_live_risk_keeps_assessed_target_and_rejects_changed_fees():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, book = narrow_snapshot(Action.SHORT)
    provider = AssessmentProvider(.50)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert decision.action is Action.SHORT
    assert decision.metadata["size_scale"] == 1
    plan = decision.metadata["jev_trade_plan"]
    assert plan["expected_net_r"] > 0
    assert decision.metadata["candidate_plans"][0]["target"] == plan["target"]
    ctx = _context(equity=20, cash=20, day_start_equity=20,
                   live_requested=True, live_armed=True, step_size=.001)
    allowed = eng.risk.evaluate(decision, snapshot, ctx, FeeQuote(.00075,.00075,"test"), book)
    assert allowed.accepted
    assert allowed.economics.target == plan["target"]
    assert allowed.economics.stop == plan["stop"]
    changed = eng.risk.evaluate(decision, snapshot, ctx, FeeQuote(.002,.002,"test"), book)
    assert "NET_RR_TOO_LOW" in changed.reject_reasons
    assert changed.economics.target == plan["target"]
    impacted = eng.risk.evaluate(decision, snapshot,
        replace(ctx, equity=7500,cash=7500,day_start_equity=7500),
        FeeQuote(.00075,.00075,"test"), book)
    assert "NET_RR_TOO_LOW" in impacted.reject_reasons
    assert impacted.economics.target == plan["target"]
    # The repair cannot bypass account/protection and loss-circuit controls.
    halted = eng.risk.evaluate(decision, snapshot, replace(ctx, consecutive_live_losses=3),
                               FeeQuote(.00075,.00075,"test"), book)
    assert "MAX_CONSECUTIVE_LIVE_LOSSES" in halted.reject_reasons
    unfunded = eng.risk.evaluate(decision, snapshot,
        replace(ctx, market_type="margin", quote_free=.1), FeeQuote(.00075,.00075,"test"), book)
    assert "INSUFFICIENT_PROTECTION_BALANCE" in unfunded.reject_reasons


@pytest.mark.asyncio
async def test_model_receives_larger_target_and_probability_refers_to_this_plan():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot()
    capture = AssessmentProvider(.65)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, capture))
    def handler(request):
        body = json.loads(request.content)
        plan = body["state"]["trade_plan"]
        assert plan["target_distance_fraction"] > .005
        assert plan["target_range_multiple"] > 1
        assert plan["target_extended_for_costs"]
        assert plan["horizon_minutes"] == 60
        assert "entry" not in plan and "target" not in plan
        assert "within horizon_minutes" in body["questions"]["trend_continuation_probability"]["instructions"]
        return httpx.Response(200,json={"answers":{k:{"noul":p} for k,p in (
            ("trend_continuation_probability",.65),("reversal_probability",.2),("false_breakout_probability",.2))}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="default",base_url="https://test.invalid",
            api_key="test-only",model="test",client=client)
        result = await provider.evaluate_market_state(capture.requests[0])
    assert result.prompt_version.endswith("_compact_market_v3")
    assert decision.metadata["jev_trade_plan"]["target"] == capture.requests[0].trade_plan["target"]


@pytest.mark.asyncio
async def test_break_cannot_confirm_a_negative_net_expectancy():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot(Action.SHORT)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features,
        context(eng, snapshot, AssessmentProvider(.30)))
    assert decision.action is Action.NO_TRADE
    assert "JEV_NON_POSITIVE_EXPECTANCY" in decision.reason_codes
    assert decision.metadata["jev_effect"] == "veto"
    assert not decision.metadata["jev_trade_plan"]["eligible"]
    assert decision.metadata["jev_trade_plan"]["expected_net_r"] < 0


@pytest.mark.asyncio
async def test_rounded_break_even_probability_cannot_become_a_positive_expectancy():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot(Action.SHORT)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features,
        context(eng, snapshot, AssessmentProvider(.45)))
    assert decision.action is Action.NO_TRADE
    assert "JEV_NON_POSITIVE_EXPECTANCY" in decision.reason_codes
    assert decision.metadata["jev_trade_plan"]["expected_net_r"] == 0
    positive = await BaselineJevStrategy().evaluate(snapshot, snapshot.features,
        context(eng, snapshot, AssessmentProvider(.46)))
    assert positive.action is Action.SHORT
    assert positive.metadata["jev_trade_plan"]["expected_net_r"] > 0


@pytest.mark.asyncio
async def test_changed_entry_keeps_price_drift_guard_and_records_both_prices():
    from app.execution.slippage import book_from_levels
    from app.domain.schemas import BookLevel
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot(Action.SHORT)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features,
        context(eng, snapshot, AssessmentProvider(.65)))
    moved_book = book_from_levels([BookLevel(price=99.8,quantity=100)],
                                 [BookLevel(price=99.82,quantity=100)],snapshot.timestamp)
    ctx = _context(equity=20,cash=20,day_start_equity=20,
                   live_requested=True,live_armed=True,step_size=.001)
    rejected = eng.risk.evaluate(decision,snapshot,ctx,FeeQuote(.00075,.00075,"test"),moved_book)
    assert "JEV_PLAN_CHANGED" in rejected.reject_reasons
    assert rejected.details["assessed_entry"] == decision.metadata["jev_trade_plan"]["entry"]
    assert rejected.details["estimated_entry"] == 99.8
    assert rejected.details["price_drift"] > rejected.details["max_price_drift"] == .001


def test_no_impossible_short_target_or_invalid_geometry_is_sent_to_model():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot(Action.SHORT)
    snapshot = snapshot.model_copy(update={"market_type":MarketType.MARGIN})
    assert candidate_plans(snapshot, Action.SHORT, eng.risk.limits, .00075)[0]["interest_estimate_per_unit"] > 0
    assert not candidate_plans(snapshot, Action.SHORT, replace(eng.risk.limits,min_net_rr=1000), .00075)
    assert not candidate_plans(snapshot.model_copy(update={"features":{**snapshot.features,"atr":0}}),
                               Action.SHORT, eng.risk.limits, .00075)


@pytest.mark.asyncio
async def test_missing_geometry_does_not_become_a_direction_only_confirmation():
    eng = cost_target_engine()
    eng.risk.update_limits(market_range_plans=False, max_hold_minutes=60)
    snapshot, _ = narrow_snapshot()
    snapshot = snapshot.model_copy(update={"features":{**snapshot.features,"range_60m":0}})
    provider = AssessmentProvider(.65)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert decision.action is Action.NO_TRADE
    assert "TRADE_PLAN_UNAVAILABLE" in decision.reason_codes
    assert not provider.requests
