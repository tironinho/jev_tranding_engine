from dataclasses import replace
from uuid import uuid4

import pytest

from app.config import BaselineWeightConfig, CombinationConfig, RiskLimits
from app.domain.enums import Action, OperatingMode, MarketType
from app.domain.enums import BASELINE_CONTEXT_CANDIDATE, BASELINE_NO_TRADE, JEV_LOW_CONTINUATION, JEV_UNAVAILABLE
from app.providers.jev.mock import MockJevProvider
from app.risk.plans import candidate_plans
from app.strategies.runners import BaselineJevStrategy, StrategyContext
from app.analytics.barriers import observed_barriers
from tests.conftest import long_snapshot, clock, engine
from tests.test_risk_limits import _context
from app.risk.engine import FeeQuote


def context(provider):
    return StrategyContext(correlation_id=uuid4(), opportunity_id=uuid4(), mode=OperatingMode.SHADOW,
        now=clock(), weights=BaselineWeightConfig(), combination=CombinationConfig(), call_model=False,
        jev=provider, risk=RiskLimits(dynamic_rr_enabled=True), round_trip_fee=.001)


class PlansProvider(MockJevProvider):
    def __init__(self, probabilities):
        super().__init__("test")
        self.probabilities, self.requests = probabilities, []

    async def evaluate_market_state(self, request):
        self.requests.append(request)
        result = await super().evaluate_market_state(request)
        probability = self.probabilities.get(request.trade_plan["plan_id"], self.probabilities.get("*"))
        if probability is None:
            raise KeyError(request.trade_plan["plan_id"])
        return result.model_copy(update={"trend_continuation_probability": probability})


@pytest.mark.asyncio
async def test_single_calculated_target_is_evaluated_without_an_rr_gate():
    snapshot, _ = long_snapshot()
    provider = PlansProvider({"*": .7})
    ctx = context(provider)
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.LONG
    assert len(provider.requests) == 1
    assert provider.requests[0].trade_plan['gross_rr'] > 3
    assert provider.requests[0].trade_plan['target'] == pytest.approx(108)
    assert decision.metadata['selected_plan_id'] == provider.requests[0].trade_plan['plan_id']
    assert len(ctx.artifacts) == 1


@pytest.mark.asyncio
async def test_no_space_to_nearest_structure_may_be_assessed_but_never_forces_a_trade():
    snapshot, _ = long_snapshot(resistance_15m=100.2)
    provider = PlansProvider({"*": .2})
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, context(provider))
    assert decision.action is Action.NO_TRADE
    assert JEV_LOW_CONTINUATION in decision.reason_codes
    assert provider.requests
    assert all(request.trade_plan["requires_structure_break"] for request in provider.requests)
    assert decision.metadata["jev_effect"] == "veto"
    assert decision.metadata["jev_continuation"] == pytest.approx(.2)
    assert decision.metadata["best_assessed_plan_id"]


@pytest.mark.asyncio
async def test_expected_rr_does_not_reject_a_confirmed_signal():
    snapshot, _ = long_snapshot(atr=.01, recent_swing_low=99.99, range_60m=1, resistance_15m=None)
    provider = PlansProvider({"*": .46})
    ctx = context(provider)
    ctx.combination = replace(ctx.combination, min_trend_continuation=.4)
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.LONG
    assert decision.metadata["jev_trade_plan"]["gross_rr"] >= 3


@pytest.mark.asyncio
async def test_all_model_failures_never_fall_back_to_unassessed_trade():
    snapshot, _ = long_snapshot()
    ctx = context(PlansProvider({}))  # KeyError for every plan
    ctx.failure_policy = 'FALLBACK_TO_BASELINE'
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.NO_TRADE
    assert JEV_UNAVAILABLE in decision.reason_codes
    assert len(ctx.artifacts) == 1
    assert all('error' in a for a in ctx.artifacts)


@pytest.mark.asyncio
async def test_coherent_subthreshold_signal_reaches_context_and_jev():
    snapshot, _ = long_snapshot()
    provider = PlansProvider({"*": .7})
    ctx = context(provider)
    ctx.weights = replace(ctx.weights, min_abs_score=.95)
    ctx.intelligence = {"data_quality": {"overall": 1.0}, "derivatives": {"oi_change_5m": .2}}

    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)

    assert decision.action is Action.LONG
    assert BASELINE_CONTEXT_CANDIDATE in decision.reason_codes
    assert decision.metadata["baseline_action"] == Action.NO_TRADE
    assert decision.metadata["candidate_action"] == Action.LONG
    assert provider.requests
    assert provider.requests[0].intelligence["derivatives"]["oi_change_5m"] == .2
    assert provider.requests[0].candidate_origin == "BASELINE_BELOW_THRESHOLD"
    assert provider.requests[0].baseline_threshold == .95
    assert provider.requests[0].candidate_threshold == .15


@pytest.mark.asyncio
async def test_divergent_subthreshold_signal_stays_blocked_before_jev():
    snapshot, _ = long_snapshot(orderflow_delta_ratio=-1, imbalance_10=-1)
    provider = PlansProvider({})
    ctx = context(provider)
    ctx.weights = replace(ctx.weights, min_abs_score=.95)

    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)

    assert decision.action is Action.NO_TRADE
    assert BASELINE_NO_TRADE in decision.reason_codes
    assert not provider.requests


@pytest.mark.asyncio
async def test_live_never_promotes_a_subthreshold_baseline_candidate():
    snapshot, _ = long_snapshot()
    provider = PlansProvider({"*": .9})
    ctx = context(provider)
    ctx.mode = OperatingMode.LIVE
    ctx.weights = replace(ctx.weights, min_abs_score=.95)

    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)

    assert decision.action is Action.NO_TRADE
    assert BASELINE_NO_TRADE in decision.reason_codes
    assert not provider.requests


@pytest.mark.asyncio
async def test_moderate_divergence_without_opposing_flow_reaches_jev():
    snapshot, _ = long_snapshot(
        breakout=False,
        breakdown=False,
        ema_alignment=-.6,
        ema_20_slope=-.0006,
        price_vs_ema20=-.003,
        return_60m=-.006,
        context_15m_slope=-.003,
        price_vs_ema20_15m=-.006,
        setup_5m_return=-.003,
    )
    class ConfirmingProvider(PlansProvider):
        async def evaluate_market_state(self, request):
            result = await super().evaluate_market_state(request)
            return result.model_copy(update={"reversal_probability": .1, "false_breakout_probability": .1})

    provider = ConfirmingProvider({"*": .7})
    ctx = context(provider)

    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)

    assert decision.action is Action.LONG
    assert BASELINE_CONTEXT_CANDIDATE in decision.reason_codes
    assert decision.metadata["class_agreement"] >= ctx.combination.candidate_min_agreement
    assert provider.requests


@pytest.mark.asyncio
async def test_subthreshold_candidate_never_uses_baseline_fallback():
    snapshot, _ = long_snapshot()
    ctx = context(PlansProvider({}))
    ctx.weights = replace(ctx.weights, min_abs_score=.95)
    ctx.failure_policy = "FALLBACK_TO_BASELINE"

    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)

    assert decision.action is Action.NO_TRADE
    assert len(ctx.artifacts) == 1


def test_three_r_floor_can_cross_near_structure_and_includes_interest_stress():
    snapshot, _ = long_snapshot(resistance_15m=104)
    snapshot = snapshot.model_copy(update={'market_type': MarketType.MARGIN})
    plans = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)
    assert plans and plans[0]['gross_rr'] == pytest.approx(3)
    assert plans[0]['target'] > 104
    assert plans[0]['requires_structure_break'] is True
    assert all(p['interest_estimate_per_unit'] > 0 and not p['interest_known'] for p in plans)


def test_confirmed_break_can_offer_three_r_beyond_trailing_hour_range():
    snapshot, _ = long_snapshot(range_60m=.4, resistance_15m=None, breakout=True)
    plans = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)

    assert plans[-1]["gross_rr"] == pytest.approx(3)
    assert plans[-1]["eligible"] is True


def test_jev_candidate_can_assess_bounded_extension_through_structure():
    snapshot, _ = long_snapshot(range_60m=.4, resistance_15m=100.2, breakout=False)

    bounded = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)
    extended = candidate_plans(
        snapshot,
        Action.LONG,
        RiskLimits(),
        .0005,
        allow_structure_extension=True,
    )

    assert bounded == extended
    assert extended[-1]["gross_rr"] == pytest.approx(3)
    assert extended[-1]["requires_structure_break"] is True
    assert extended[-1]["eligible"] is True


def test_recent_break_level_does_not_collapse_future_targets():
    snapshot, _ = long_snapshot(recent_high=100.05, resistance_15m=104)

    plans = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)

    assert plans
    assert any(plan['eligible'] for plan in plans)
    assert max(plan['target'] for plan in plans) > snapshot.features['recent_high']
    assert plans[0]['gross_rr'] == pytest.approx(3)
    assert plans[0]['target'] > snapshot.features['resistance_15m']


@pytest.mark.asyncio
async def test_risk_does_not_reject_rr_expectancy_but_keeps_aggregate_loss_limit():
    snapshot, book = long_snapshot()
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, context(PlansProvider({'*': .7})))
    eng = engine()
    decision.metadata['jev_stress_probability'] = .1
    risk = eng.risk.evaluate(decision, snapshot, _context(), FeeQuote(.0005,.0005,'test'), book)
    assert risk.accepted
    risk = eng.risk.evaluate(decision, snapshot, _context(open_stop_risk=200), FeeQuote(.0005,.0005,'test'), book)
    assert 'MAX_PORTFOLIO_STOP_RISK' in risk.reject_reasons


def test_production_plan_uses_three_r_floor_and_never_rr_rejects():
    snapshot, _ = long_snapshot()
    limits = RiskLimits(dynamic_rr_enabled=True, rr_target_multiple=3.0)

    plans = candidate_plans(snapshot, Action.LONG, limits, .00075)

    assert len(plans) == 1
    assert plans[0]['eligible'] is True
    assert plans[0]['gross_rr'] >= 3


def bar(start, high, low, close):
    return [start, '100', str(high), str(low), str(close), '0', start + 59999]


def test_barrier_audit_excludes_partial_future_candles_and_reports_ambiguity():
    bars = [bar(0,110,90,100), bar(60000,101,99,100), bar(120000,110,90,100)]
    result = observed_barriers(bars,start_ms=1000,end_ms=150000,entry=100,stop=95,target=105,side='LONG')
    assert result['bars'] == 1 and result['observed_barrier'] == 'NO_BARRIER_OBSERVED'
    assert not result['complete_horizon']
    result = observed_barriers(bars,start_ms=0,end_ms=180000,entry=100,stop=95,target=105,side='LONG')
    assert result['complete_horizon'] and result['observed_barrier'] == 'AMBIGUOUS'
