from dataclasses import replace
from uuid import uuid4

import pytest

from app.config import BaselineWeightConfig, CombinationConfig, RiskLimits
from app.domain.enums import Action, OperatingMode, MarketType
from app.domain.enums import BASELINE_CONTEXT_CANDIDATE, BASELINE_NO_TRADE, JEV_EXPECTANCY_TOO_LOW, JEV_UNAVAILABLE
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
        return result.model_copy(update={"trend_continuation_probability": self.probabilities[request.trade_plan["plan_id"]]})


@pytest.mark.asyncio
async def test_independent_target_probabilities_choose_ev_not_largest_rr():
    snapshot, _ = long_snapshot()
    provider = PlansProvider({"rr_2": .9, "rr_2.5": .65, "rr_3": .41})
    ctx = context(provider)
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.LONG
    assert len(provider.requests) == 3
    assert len({r.trade_plan['target'] for r in provider.requests}) == 3
    assert decision.metadata['selected_plan_id'] == 'rr_2'
    assert len(ctx.artifacts) == 3
    assert decision.metadata['jev_stress_probability'] == pytest.approx(.85)


@pytest.mark.asyncio
async def test_no_space_to_nearest_structure_may_be_assessed_but_never_forces_a_trade():
    snapshot, _ = long_snapshot(resistance_15m=100.2)
    provider = PlansProvider({"rr_2": .2, "rr_2.5": .2, "rr_3": .2})
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, context(provider))
    assert decision.action is Action.NO_TRADE
    assert 'NO_POSITIVE_EXPECTANCY_PLAN' in decision.reason_codes
    assert provider.requests
    assert all(request.trade_plan["requires_structure_break"] for request in provider.requests)
    assert decision.metadata["jev_effect"] == "veto"
    assert decision.metadata["jev_continuation"] == pytest.approx(.2)
    assert decision.metadata["best_assessed_plan_id"]


@pytest.mark.asyncio
async def test_barely_positive_uncalibrated_expectancy_is_rejected():
    snapshot, _ = long_snapshot(atr=.01, recent_swing_low=99.99, range_60m=1, resistance_15m=None)
    provider = PlansProvider({"rr_2": .2, "rr_2.5": .2, "rr_3": .46})
    ctx = context(provider)
    ctx.combination = replace(ctx.combination, min_trend_continuation=.4)
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.NO_TRADE
    assert JEV_EXPECTANCY_TOO_LOW in decision.reason_codes
    assert decision.metadata["jev_trade_plan"]["expected_net_r"] < .15


@pytest.mark.asyncio
async def test_all_model_failures_never_fall_back_to_unassessed_trade():
    snapshot, _ = long_snapshot()
    ctx = context(PlansProvider({}))  # KeyError for every plan
    ctx.failure_policy = 'FALLBACK_TO_BASELINE'
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, ctx)
    assert decision.action is Action.NO_TRADE
    assert JEV_UNAVAILABLE in decision.reason_codes
    assert len(ctx.artifacts) == 3
    assert all('error' in a for a in ctx.artifacts)


@pytest.mark.asyncio
async def test_coherent_subthreshold_signal_reaches_context_and_jev():
    snapshot, _ = long_snapshot()
    provider = PlansProvider({"rr_2": .9, "rr_2.5": .8, "rr_3": .7})
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

    provider = ConfirmingProvider({"rr_2": .9, "rr_2.5": .8, "rr_3": .7})
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
    assert len(ctx.artifacts) == 3


def test_candidates_cannot_cross_structure_and_include_unknown_interest_stress():
    snapshot, _ = long_snapshot(resistance_15m=104)
    snapshot = snapshot.model_copy(update={'market_type': MarketType.MARGIN})
    plans = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)
    assert plans and max(p['target'] for p in plans) <= 104
    assert all(p['interest_estimate_per_unit'] > 0 and not p['interest_known'] for p in plans)
    assert len({p['target'] for p in plans}) == len(plans)


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

    assert max(plan["target"] for plan in bounded) <= 100.2
    assert extended[-1]["gross_rr"] == pytest.approx(3)
    assert extended[-1]["requires_structure_break"] is True
    assert extended[-1]["eligible"] is True


def test_recent_break_level_does_not_collapse_future_targets():
    snapshot, _ = long_snapshot(recent_high=100.05, resistance_15m=104)

    plans = candidate_plans(snapshot, Action.LONG, RiskLimits(), .0005)

    assert plans
    assert any(plan['eligible'] for plan in plans)
    assert max(plan['target'] for plan in plans) > snapshot.features['recent_high']
    assert max(plan['target'] for plan in plans) <= snapshot.features['resistance_15m']


@pytest.mark.asyncio
async def test_risk_rechecks_stressed_probability_and_aggregate_loss():
    snapshot, book = long_snapshot()
    decision = await BaselineJevStrategy().evaluate(snapshot, {}, context(PlansProvider({'rr_2': .9, 'rr_2.5': .8, 'rr_3': .7})))
    eng = engine()
    decision.metadata['jev_stress_probability'] = .1
    risk = eng.risk.evaluate(decision, snapshot, _context(), FeeQuote(.0005,.0005,'test'), book)
    assert 'JEV_NONPOSITIVE_EXPECTANCY' in risk.reject_reasons
    decision.metadata['jev_stress_probability'] = .34
    risk = eng.risk.evaluate(decision, snapshot, _context(), FeeQuote(.0005,.0005,'test'), book)
    assert JEV_EXPECTANCY_TOO_LOW in risk.reject_reasons
    assert 0 < risk.details['expected_net_r'] < risk.details['minimum_expected_net_r']
    decision.metadata['jev_stress_probability'] = .85
    risk = eng.risk.evaluate(decision, snapshot, _context(open_stop_risk=200), FeeQuote(.0005,.0005,'test'), book)
    assert 'MAX_PORTFOLIO_STOP_RISK' in risk.reject_reasons


def bar(start, high, low, close):
    return [start, '100', str(high), str(low), str(close), '0', start + 59999]


def test_barrier_audit_excludes_partial_future_candles_and_reports_ambiguity():
    bars = [bar(0,110,90,100), bar(60000,101,99,100), bar(120000,110,90,100)]
    result = observed_barriers(bars,start_ms=1000,end_ms=150000,entry=100,stop=95,target=105,side='LONG')
    assert result['bars'] == 1 and result['observed_barrier'] == 'NO_BARRIER_OBSERVED'
    assert not result['complete_horizon']
    result = observed_barriers(bars,start_ms=0,end_ms=180000,entry=100,stop=95,target=105,side='LONG')
    assert result['complete_horizon'] and result['observed_barrier'] == 'AMBIGUOUS'
