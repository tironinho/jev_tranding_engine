from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.config import CombinationConfig
from app.domain.enums import Action
from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import FIRST_TEN_HISTORICAL_PROMPT, JevAssessment
from app.risk.economics import plan_geometry
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.rules import apply_jev_veto, break_size_scale
from app.strategies.runners import BaselineJevStrategy, _continuation_floor
from tests.conftest import settings, long_snapshot, attach_book
from tests.test_initial_live_profile import AssessmentProvider, context

PROFILE = "live_first_ten_2026_10_09"


def first_ten_engine():
    eng = TradingEngine(settings())
    eng.risk.update_limits(time_exit_enabled=True, min_net_rr=1.5, max_trade_notional_fraction=0,
                          max_portfolio_notional_fraction=0, min_net_target_return=0)
    # Reconstruct the exact tenth-trade policy, before the cost-target correction.
    eng.weights = replace(eng.weights, version="baseline_first_ten_v1")
    eng.combination = replace(eng.combination, baseline_version="baseline_first_ten_v1", jev_input_profile="legacy", jev_rule_version="jev_break_size_v1")
    eng.risk.update_limits(target_rule_version=PROFILE, cost_aware_target_floor=False, market_range_plans=False, max_hold_minutes=60, live_longs_enabled=True, extend_target_for_costs=True)
    eng.risk_limits = eng.risk.limits
    return eng


def test_repository_uses_tenth_trade_policy_and_wider_hour_is_not_capped():
    eng = first_ten_engine()
    snapshot, _ = long_snapshot(range_60m=30)
    assert eng.weights.version == "baseline_first_ten_v1"
    assert eng.weights.min_abs_score == .45
    assert eng.combination.jev_request_profile == PROFILE
    assert eng.combination.min_short_continuation == .4
    assert eng.combination.min_trend_continuation == .55
    assert eng.risk.limits.min_net_rr == 1.5
    assert not eng.risk.limits.dynamic_rr_enabled
    geometry = plan_geometry(Action.LONG, 100, snapshot.features, eng.risk.limits)
    assert geometry.target == 130
    assert score_baseline(snapshot, eng.weights).market_class.labels


def test_short_fee_floor_and_break_size_reproduce_historical_rule():
    eng = first_ten_engine()
    snapshot = SimpleNamespace(price=100, features={"atr": .001})
    ctx = SimpleNamespace(risk=eng.risk.limits, combination=eng.combination, round_trip_fee=.0015)
    floor = _continuation_floor(snapshot, ctx, Action.SHORT)
    assert floor == pytest.approx((1+.0015/.0025)/3.5)
    assessment = JevAssessment(provider="mock", is_mock=True, provider_version="test",
        prompt_version="test", trend_continuation_probability=.43,
        reversal_probability=.3, false_breakout_probability=.2)
    blocked, _, reasons = apply_jev_veto(Action.SHORT, .6, assessment, eng.combination,
        breakout=False, min_continuation=floor)
    assert blocked is Action.NO_TRADE
    assert "JEV_LOW_CONTINUATION" in reasons
    allowed, _, _ = apply_jev_veto(Action.SHORT, .6, assessment, eng.combination,
        breakout=True, min_continuation=floor)
    assert allowed is Action.SHORT
    assert break_size_scale(.43, floor, True) == pytest.approx(.43/floor)
    reversal = assessment.model_copy(update={"reversal_probability": .7})
    assert apply_jev_veto(Action.SHORT, .6, reversal, eng.combination, True, floor)[0] is Action.NO_TRADE
    false_break = assessment.model_copy(update={"false_breakout_probability": .9})
    assert apply_jev_veto(Action.SHORT, .6, false_break, eng.combination, True, floor)[0] is Action.NO_TRADE
    modern = CombinationConfig()
    assert apply_jev_veto(Action.SHORT, .6, assessment, modern, True)[0] is Action.NO_TRADE


@pytest.mark.asyncio
async def test_non_break_short_can_confirm_at_40_when_cost_floor_is_lower():
    eng = first_ten_engine()
    snapshot, _ = long_snapshot(ema_alignment=-1, ema_20_slope=-.001,
        price_vs_ema20=-.002, rsi=36, roc=-.003, orderflow_delta_ratio=-.45,
        imbalance_10=-.3, range_position=.18, breakout=False, breakdown=False)
    provider = AssessmentProvider(.40)
    result = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert result.action is Action.SHORT
    assert result.metadata["jev_required_continuation"] == .4
    assert result.metadata["size_scale"] == 1
    assert provider.requests[0].trade_plan is None
    assert provider.requests[0].baseline_class != "unavailable"


@pytest.mark.asyncio
async def test_first_ten_provider_sends_class_and_drops_low_quality_external_context():
    eng = first_ten_engine()
    snapshot, _ = long_snapshot()
    capture = AssessmentProvider(.6)
    await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, capture))
    def handler(request):
        import json
        payload = json.loads(request.content)
        state = payload["state"]
        assert state["baseline_class"] != "unavailable"
        assert "BREAKOUT" in state["baseline_labels"]
        assert "trade_plan" not in state
        assert "intelligence" not in state
        text = payload["questions"]["trend_continuation_probability"]["instructions"]
        assert "weighted scores" in text and "onchain" not in text
        assert "horizon_minutes" not in text
        return httpx.Response(200,json={"answers":{k:{"noul":p} for k,p in (
            ("trend_continuation_probability",.6),("reversal_probability",.3),("false_breakout_probability",.2))}})
    req = capture.requests[0].model_copy(update={"intelligence": {
        "data_quality":{"overall":.2},"binance_agent":{"quality":1}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="default",base_url="https://test.invalid",
            api_key="test-only",model="test",client=client)
        result = await provider.evaluate_market_state(req)
    assert result.prompt_version == FIRST_TEN_HISTORICAL_PROMPT


@pytest.mark.asyncio
async def test_confirmed_break_with_low_continuation_opens_smaller_paper_position():
    async def run(probability):
        eng = first_ten_engine()
        snapshot, book = long_snapshot()
        attach_book(eng, book)
        eng.jev = AssessmentProvider(probability)
        await eng.evaluate_snapshot(snapshot)
        pos = next(iter(eng.accounts.accounts["baseline_jev"].positions.values()))
        assert pos.mode == "paper"
        assert pos.target == pytest.approx(pos.entry_price+snapshot.features["range_60m"])
        state=eng.states[snapshot.symbol]
        state.best_bid=pos.entry_price+.5*(pos.entry_price-pos.stop)
        state.best_ask=state.best_bid+.02
        await eng.manage_positions(snapshot.symbol, as_of=snapshot.timestamp+timedelta(minutes=61))
        assert str(pos.position_id) in eng.accounts.accounts["baseline_jev"].positions
        return pos
    reduced=await run(.37)
    normal=await run(.65)
    assert reduced.quantity < normal.quantity
    assert reduced.quantity/normal.quantity == pytest.approx(.37/.55,rel=.01)


@pytest.mark.asyncio
async def test_zero_continuation_cannot_turn_zero_size_into_a_full_order():
    eng=first_ten_engine()
    snapshot,book=long_snapshot()
    attach_book(eng,book)
    eng.jev=AssessmentProvider(0)
    await eng.evaluate_snapshot(snapshot)
    assert not eng.accounts.accounts["baseline_jev"].positions



def test_compact_market_observations_survive_degraded_external_provider():
    from app.providers.jev.real import _state_for_model
    state = _state_for_model({"evaluation_profile": PROFILE,
        "features": {"rsi": 61, "return_5m": .002, "volume_ratio": 1.5,
                     "taker_flow_5m": .4, "imbalance_10": .2},
        "intelligence": {"data_quality": {"overall": .2}}}, .7)
    assert state["market_context"]["rsi"] == 61
    assert state["market_context"]["taker_flow_5m"] == .4
    assert state["market_context"]["return_15m"] is None
    assert len(state["market_context"]) == 10
    assert "intelligence" not in state


@pytest.mark.asyncio
async def test_historical_profile_sends_borderline_candidate_to_jev_read_only():
    from dataclasses import replace
    from app.domain.enums import OperatingMode
    eng = first_ten_engine()
    eng.weights = replace(eng.weights, min_abs_score=.95)
    eng.combination = replace(eng.combination, review_below_threshold=True)
    snapshot, _ = long_snapshot()
    provider = AssessmentProvider(.9)
    ctx = context(eng, snapshot, provider)
    ctx.mode = OperatingMode.LIVE
    result = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, ctx)
    assert len(provider.requests) == 1
    assert provider.requests[0].candidate_origin == "BASELINE_BELOW_THRESHOLD"
    assert result.action is Action.NO_TRADE
    assert result.metadata["baseline_action"] == "NO_TRADE"
    assert result.metadata["jev_effect"] == "shadow_confirm"
    assert result.metadata["jev_evaluation_mode"] == "READ_ONLY_SHADOW"


@pytest.mark.asyncio
async def test_historical_profile_review_can_be_disabled_and_bad_data_stays_blocked():
    from dataclasses import replace
    eng = first_ten_engine()
    eng.weights = replace(eng.weights, min_abs_score=.95)
    eng.combination = replace(eng.combination, review_below_threshold=False)
    snapshot, _ = long_snapshot()
    provider = AssessmentProvider(.9)
    result = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert not provider.requests
    assert result.metadata["jev_effect"] == "idle"
    eng.combination = replace(eng.combination, review_below_threshold=True)
    snapshot, _ = long_snapshot(spread_bps=20)
    result = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert not provider.requests
    assert result.action is Action.NO_TRADE
