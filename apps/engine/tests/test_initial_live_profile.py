import httpx
import pytest
from datetime import timedelta
from dataclasses import replace

from app.config import BaselineWeightConfig
from app.domain.enums import Action, OperatingMode, NET_RR_TOO_LOW
from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import JevAssessment
from app.risk.economics import plan_geometry
from app.risk.engine import FeeQuote, RiskContext
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.runners import StrategyContext, BaselineJevStrategy
from app.execution.paper import position_exit
from tests.conftest import settings, long_snapshot, attach_book


def initial_engine():
    # Preserve regression coverage of the earlier first-entry profile while
    # the repository default now selects the explicitly requested tenth trade.
    eng = TradingEngine(settings())
    eng.risk.update_limits(time_exit_enabled=True, min_net_rr=1.5, max_trade_notional_fraction=0,
                          max_portfolio_notional_fraction=0, min_net_target_return=0)
    eng.weights = BaselineWeightConfig(version="baseline_weights_v1", min_abs_score=.45)
    eng.combination = replace(eng.combination, jev_request_profile="initial_live_2026_10_08",
        jev_input_profile="legacy", min_short_continuation=.55, jev_rule_version="jev_break_size_v1")
    eng.risk.update_limits(target_rule_version="initial_live_2026_10_08", cost_aware_target_floor=False,
                          market_range_plans=False, max_hold_minutes=60, live_longs_enabled=True, extend_target_for_costs=True)
    eng.risk_limits = eng.risk.limits
    return eng


def test_initial_profile_uses_the_historical_range_cap_and_net_rr_gate():
    eng = initial_engine()
    snapshot, _ = long_snapshot(range_60m=.5)
    geometry = plan_geometry(Action.LONG, 100, snapshot.features, eng.risk.limits)
    assert eng.weights.version == "baseline_weights_v1"
    assert eng.weights.min_abs_score == .45
    assert eng.combination.jev_request_profile == "initial_live_2026_10_08"
    assert not eng.risk.limits.dynamic_rr_enabled
    assert eng.risk.limits.live_longs_enabled
    assert eng.risk.limits.min_net_rr == 1.5
    assert geometry.target == 100.5
    larger = plan_geometry(Action.LONG, 100,
        {**snapshot.features, "range_60m": 30}, eng.risk.limits)
    assert larger.target == pytest.approx(100 + (100 - larger.stop) * 2.5)


def test_initial_score_does_not_acquire_the_later_15m_classifier_filters():
    eng = initial_engine()
    snapshot, _ = long_snapshot(context_15m_slope=-.05, price_vs_ema20_15m=-.05,
        setup_5m_return=-.05, breakout=False)
    initial = score_baseline(snapshot, eng.weights)
    modern = score_baseline(snapshot, BaselineWeightConfig())
    assert initial.action is Action.LONG
    assert initial.scores["trend_score"] == pytest.approx(.88)
    assert modern.scores["trend_score"] < initial.scores["trend_score"]
    assert "CLASS_DIVERGENT" not in initial.reason_codes


class AssessmentProvider:
    def __init__(self, probability):
        self.probability = probability
        self.requests = []

    async def evaluate_market_state(self, request):
        self.requests.append(request)
        return JevAssessment(provider="mock", is_mock=True,
            provider_version="test", prompt_version=request.prompt_version,
            trend_continuation_probability=self.probability,
            reversal_probability=.2, false_breakout_probability=.2)


def context(eng, snapshot, provider):
    return StrategyContext(correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id, mode=OperatingMode.LIVE,
        now=snapshot.timestamp, weights=eng.weights, combination=eng.combination,
        call_model=True, jev=provider, risk=eng.risk.limits, round_trip_fee=.0015)


@pytest.mark.asyncio
async def test_initial_live_only_asks_for_valid_baseline_signals_and_keeps_55_percent_floor():
    eng = initial_engine()
    snapshot, _ = long_snapshot()
    provider = AssessmentProvider(.54)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert decision.action is Action.NO_TRADE
    assert "JEV_LOW_CONTINUATION" in decision.reason_codes
    request = provider.requests[0]
    assert request.trade_plan is None
    assert request.evaluation_profile == "initial_live_2026_10_08"
    provider.probability = .55
    allowed = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert allowed.action is Action.LONG
    assert allowed.metadata.get("jev_trade_plan") is None
    assert "DYNAMIC_PLAN_SELECTED" not in allowed.reason_codes
    weak = snapshot.model_copy(update={"features": {**snapshot.features,
        "ema_alignment": 0, "ema_20_slope": 0, "price_vs_ema20": 0,
        "rsi": 55, "roc": 0, "volume_ratio": 1, "orderflow_delta_ratio": .1,
        "imbalance_10": 0, "range_position": .5, "breakout": False}})
    before = len(provider.requests)
    refused = await BaselineJevStrategy().evaluate(weak, weak.features, context(eng, weak, provider))
    assert refused.action is Action.NO_TRADE
    assert len(provider.requests) == before


@pytest.mark.asyncio
async def test_initial_provider_sends_the_original_compact_question_contract():
    eng = initial_engine()
    snapshot, _ = long_snapshot(range_60m_frac=.005, taker_flow_1m=.4, return_60m=.002)
    capture = AssessmentProvider(.6)
    await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, capture))
    def handler(request):
        import json
        payload = json.loads(request.content)
        assert set(payload["state"]) == {"symbol", "market_type", "baseline_action",
            "baseline_confidence", "baseline_scores", "taker_flow_1m", "return_60m",
            "range_60m", "breakout", "breakdown"}
        assert payload["questions"]["trend_continuation_probability"]["instructions"] == (
            "The `baseline_action` side reaches the target before the stop.")
        assert payload["state"]["range_60m"] == .005
        return httpx.Response(200, json={"answers": {name: {"noul": p} for name, p in
            (("trend_continuation_probability", .6), ("reversal_probability", .2), ("false_breakout_probability", .2))}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="new-default", base_url="https://test.invalid",
            api_key="test-only", model="test", client=client)
        assessment = await provider.evaluate_market_state(capture.requests[0])
    assert assessment.prompt_version == "jev_initial_live_2026_10_08"
    assert assessment.trend_continuation_probability == .6


@pytest.mark.asyncio
async def test_initial_profile_opens_paper_and_rejects_a_target_that_cannot_pay_costs():
    eng = initial_engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.jev = AssessmentProvider(.65)
    await eng.evaluate_snapshot(snapshot)
    positions = eng.accounts.accounts["baseline_jev"].positions
    assert len(positions) == 1
    assert not eng.accounts.accounts["baseline"].positions
    position = next(iter(positions.values()))
    assert position.mode == "paper"
    assert (position.target-position.entry_price)/(position.entry_price-position.stop) == pytest.approx(2.5)
    assert position.initial_net_risk > 0
    # Exercise the actual management path: the initial fixed stop is below
    # breakeven, but a profitable mark must still survive the one-hour clock.
    state = eng.states[snapshot.symbol]
    state.best_bid = position.entry_price + .5 * (position.entry_price-position.stop)
    state.best_ask = state.best_bid + .02
    state.last_price = state.best_bid
    await eng.manage_positions(snapshot.symbol, as_of=snapshot.timestamp+timedelta(minutes=61))
    assert str(position.position_id) in eng.accounts.accounts["baseline_jev"].positions
    # A very small range must not become a free pass when costs exceed its reward.
    next_snapshot = snapshot.model_copy(update={"features": {**snapshot.features, "range_60m": .05}})
    decision = await BaselineJevStrategy().evaluate(next_snapshot, next_snapshot.features,
        context(eng, next_snapshot, AssessmentProvider(.65)))
    risk_context = RiskContext(equity=10_000, cash=10_000, day_start_equity=10_000,
        realized_pnl_today=0, open_positions=0, symbol_exposure_notional=0,
        total_exposure_notional=0, has_position_on_symbol=False, last_entry_at=None,
        now=snapshot.timestamp, trading_enabled=True, persistence_ok=True,
        live_requested=True, live_armed=True, market_type="futures", step_size=.001)
    blocked = eng.risk.evaluate(decision, next_snapshot, risk_context, FeeQuote(.00075,.00075,"test"), book)
    assert NET_RR_TOO_LOW in blocked.reject_reasons


def test_initial_time_exit_closes_losers_after_60m_and_preserves_winners():
    eng = initial_engine()
    assert eng.risk.limits.time_exit_enabled
    assert not eng.risk.limits.step_stop_to_breakeven
    assert position_exit(Action.LONG, 99, 99.01, 98, 104, 60,
        eng.risk.limits.max_hold_minutes, entry=100) == "TIME"
    assert position_exit(Action.LONG, 101, 101.01, 98, 104, 60,
        eng.risk.limits.max_hold_minutes, entry=100) is None


def test_saved_runtime_does_not_replace_the_restored_threshold_or_positions():
    eng = initial_engine()
    eng.apply_runtime({"risk": {"min_net_rr": 2.25}})
    assert eng.risk.limits.min_net_rr == 1.5
    assert eng.risk.limits.rr_target_multiple == 2.5
    assert eng.risk.limits.live_longs_enabled
