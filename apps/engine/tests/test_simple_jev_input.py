import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest

from app.domain.enums import Action, NET_RR_TOO_LOW, MAX_CONSECUTIVE_LIVE_LOSSES
from app.providers.jev.real import RealJevProvider, _state_for_model
from app.providers.jev.schemas import SIMPLE_MARKET_INPUT, SIMPLE_MARKET_PROMPT
from app.risk.engine import FeeQuote
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.runners import BaselineJevStrategy
from tests.conftest import attach_book, clock, long_snapshot, settings
from tests.test_initial_live_profile import AssessmentProvider, context
from tests.test_risk_limits import _context


def candidate(side=Action.LONG, **overrides):
    features = {"taker_flow_1m": .45, "taker_flow_5m": .3}
    if side is Action.SHORT:
        features.update(ema_alignment=-1, ema_20_slope=-.001, price_vs_ema20=-.002,
            rsi=36, roc=-.003, orderflow_delta_ratio=-.45, taker_flow_1m=-.45,
            taker_flow_5m=-.3, imbalance_10=-.3, range_position=.18,
            breakout=False, breakdown=True)
    features.update(overrides)
    return long_snapshot(**features)


async def evaluate(eng, snapshot, probability=.43, intelligence=None):
    provider = AssessmentProvider(probability)
    ctx = context(eng, snapshot, provider)
    ctx.round_trip_fee = .002
    ctx.intelligence = intelligence
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, ctx)
    return decision, provider


def test_active_configuration_changes_input_and_volume_only():
    eng = TradingEngine(settings())
    policy = eng.status()["decision_policy"]
    assert policy["baseline_version"] == "baseline_first_ten_v2"
    assert policy["input_profile"] == SIMPLE_MARKET_INPUT
    assert policy["prompt_version"] == SIMPLE_MARKET_PROMPT
    assert policy["request_profile"] == "live_first_ten_2026_10_09"
    assert policy["jev_rule_version"] == "jev_break_size_v1"
    assert not any(policy[k] for k in ("cost_aware_target_floor", "market_range_plans",
                                      "jev_expectancy_gate", "review_below_threshold"))
    assert policy["max_hold_minutes"] == 60 and policy["min_net_rr"] == 1.5
    assert eng.combination.min_trend_continuation == .55
    assert eng.combination.min_short_continuation == .4
    assert eng.risk.limits.max_consecutive_live_losses == 10


@pytest.mark.parametrize("flow", [-.8, .8])
def test_active_baseline_keeps_seven_components_and_weak_volume_never_inverts_flow(flow):
    eng = TradingEngine(settings())
    snapshot, _ = candidate(volume_ratio=.46, taker_flow_1m=flow)
    current = score_baseline(snapshot, eng.weights)
    historical = score_baseline(snapshot, replace(eng.weights, version="baseline_first_ten_v1"))
    assert len(current.scores) == 7
    assert current.scores["volume_score"] == 0
    assert historical.scores["volume_score"]*flow < 0
    assert all(current.scores[k] == historical.scores[k] for k in current.scores if k != "volume_score")


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [Action.LONG, Action.SHORT])
async def test_actual_http_body_has_seven_groups_and_raw_answers_survive(side):
    eng = TradingEngine(settings())
    snapshot, _ = candidate(side)
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        state = body["state"]
        assert set(state) == {"symbol", "market_type", "candidate_side", "candle_minutes", "evidence", "trade"}
        assert state["candidate_side"] == side.value
        assert set(state["evidence"]) == {"trend", "momentum", "volume", "flow", "structure", "volatility", "liquidity"}
        assert state["evidence"]["flow"]["taker_5m"] == snapshot.features["taker_flow_5m"]
        assert state["evidence"]["volatility"]["atr_14_pct"] == pytest.approx(.4)
        assert state["evidence"]["liquidity"]["spread_bps"] == 2
        assert state["evidence"]["structure"]["event"] == ("BREAKOUT" if side is Action.LONG else "BREAKDOWN")
        assert "extension_from_break_level_atr" in state["evidence"]["structure"]
        assert state["trade"]["target_distance_pct"] > 0
        assert state["trade"]["stop_distance_pct"] > 0
        rule = state["trade"]["time_exit"]
        assert rule["rule"] == "EXIT_IF_NET_PNL_NON_POSITIVE"
        assert rule["after_minutes"] == 60
        assert rule["estimated_breakeven_distance_pct"] > 0
        assert "onchain" not in request.content.decode()
        assert "baseline_confidence" not in request.content.decode()
        assert len(body["questions"]) == 3
        assert "review time is not a deadline" in body["questions"]["trend_continuation_probability"]["instructions"]
        return httpx.Response(200, json={"answers": {k: {"noul": p} for k, p in (
            ("trend_continuation_probability", .27), ("reversal_probability", .31),
            ("false_breakout_probability", .32))}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="default", base_url="https://test.invalid",
            api_key="test-only", model="test", client=client)
        ctx = context(eng, snapshot, provider)
        ctx.round_trip_fee = .002
        ctx.intelligence = {"onchain": {"support_long": 1, "evidence": 1}, "data_quality": {"overall": 1}}
        result = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, ctx)
    assert len(bodies) == 1
    assert result.prompt_version == SIMPLE_MARKET_PROMPT
    assert result.metadata["jev_continuation"] == .27
    assert result.metadata["jev_reversal"] == .31
    assert result.metadata["jev_false_breakout"] == .32
    assert result.metadata["jev_trade_plan"] is None
    assert result.metadata["jev_observation_plan"]["estimate_only"]
    assert ctx.artifacts[-1]["response"]["sent_request"] == bodies[0]


@pytest.mark.asyncio
async def test_mock_keeps_its_label_and_the_selected_input_version():
    from app.providers.jev.mock import MockJevProvider
    eng = TradingEngine(settings())
    snapshot, _ = candidate()
    _, capture = await evaluate(eng, snapshot)
    result = await MockJevProvider("default").evaluate_market_state(capture.requests[0])
    assert result.is_mock and result.prompt_version == SIMPLE_MARKET_PROMPT


@pytest.mark.asyncio
async def test_explicit_plan_deadline_is_described_without_changing_the_plan():
    from app.providers.jev.simple import observation_plan
    eng = TradingEngine(settings())
    snapshot, _ = candidate()
    _, capture = await evaluate(eng, snapshot)
    existing_plan = {"entry": 100.01, "stop": 99, "target": 103,
                     "target_policy": "market_range_v1", "max_hold_minutes": 240}
    before = dict(existing_plan)
    observed = observation_plan(snapshot, Action.LONG, eng.risk.limits, .002, existing_plan)
    payload = capture.requests[0].model_copy(update={"observation_plan": observed}).model_dump(mode="json")
    assert _state_for_model(payload, .7)["trade"]["time_exit"] == {"rule": "EXIT_AT_DEADLINE", "after_minutes": 240}
    assert existing_plan == before


@pytest.mark.asyncio
async def test_optional_dynamic_policy_describes_each_of_its_own_candidates():
    from tests.conftest import market_range_engine
    from tests.test_market_range_plans import snapshot as market_snapshot
    eng = market_range_engine()
    eng.combination = replace(eng.combination, jev_input_profile=SIMPLE_MARKET_INPUT,
                              jev_request_profile="plan_outcome_v3")
    eng.risk.update_limits(dynamic_rr_enabled=True)
    snapshot, _ = market_snapshot()
    _, provider = await evaluate(eng, snapshot, probability=.9)
    assert len(provider.requests) >= 2
    for request in provider.requests:
        assert request.observation_plan["target"] == request.trade_plan["target"]
        assert request.observation_plan["stop"] == request.trade_plan["stop"]


@pytest.mark.asyncio
async def test_missing_external_sources_cannot_block_the_exchange_only_input():
    eng = TradingEngine(settings())
    snapshot, _ = candidate()
    decision, provider = await evaluate(eng, snapshot, intelligence={
        "gate": "NO_TRADE", "data_quality": {"overall": .1},
        "binance_agent": {"state": "BEARISH", "quality": 1}})
    assert len(provider.requests) == 1
    assert provider.requests[0].intelligence is None
    assert decision.action is Action.LONG
    assert "INTELLIGENCE_QUALITY" not in decision.reason_codes
    assert "binance_agent_state" not in decision.metadata


@pytest.mark.asyncio
async def test_pipeline_does_not_build_unused_external_context():
    eng = TradingEngine(settings())
    snapshot, _ = candidate()
    eng.jev = AssessmentProvider(.65)
    eng.intelligence.context_for = Mock(side_effect=AssertionError("unused external context"))
    eng.intelligence.note_marks = Mock()
    import time
    decision, ctx, _ = await eng._decide("baseline_jev", snapshot, snapshot.snapshot_id,
                                       snapshot.snapshot_id, time.perf_counter(), waited_ms=0)
    assert decision.metadata["jev_effect"] == "confirm"
    assert ctx.intelligence is None
    eng.intelligence.context_for.assert_not_called()
    eng.intelligence.note_marks.assert_not_called()


@pytest.mark.asyncio
async def test_optional_missing_and_invalid_observations_do_not_become_zero():
    eng = TradingEngine(settings())
    snapshot, _ = candidate()
    _, provider = await evaluate(eng, snapshot)
    request = provider.requests[0].model_dump(mode="json")
    request["features"].update(taker_flow_1m=0, taker_flow_5m=float("nan"),
        volume_ratio=float("inf"), imbalance_10=.9, spread_bps=-1,
        roc=True, breakout=None, breakdown=None)
    body = _state_for_model(request, .7)
    assert body["evidence"]["flow"] == {"taker_1m": 0}
    assert body["evidence"]["volume"] == {}
    assert body["evidence"]["liquidity"] == {}
    assert "roc_10m_pct" not in body["evidence"]["momentum"]
    assert body["evidence"]["structure"] == {"event": "UNKNOWN"}
    json.dumps(body, allow_nan=False)


@pytest.mark.asyncio
async def test_no_break_has_no_invented_structural_level():
    eng = TradingEngine(settings())
    snapshot, _ = candidate(breakout=False, breakdown=False)
    _, provider = await evaluate(eng, snapshot, probability=.9)
    body = _state_for_model(provider.requests[0].model_dump(mode="json"), .7)
    assert body["evidence"]["structure"] == {"event": "NONE"}


@pytest.mark.asyncio
async def test_unavailable_geometry_has_a_clear_reason_and_never_calls_provider():
    eng = TradingEngine(settings())
    snapshot, _ = candidate(range_60m=0)
    decision, provider = await evaluate(eng, snapshot)
    assert not provider.requests
    assert decision.action is Action.NO_TRADE
    assert "JEV_INPUT_UNAVAILABLE" in decision.reason_codes
    assert "HOUR_RANGE_UNAVAILABLE" in decision.metadata["input_error"]


@pytest.mark.asyncio
async def test_small_range_reaches_model_then_normal_risk_still_rejects_it():
    eng = TradingEngine(settings())
    snapshot, book = candidate(atr=.01, atr_normalized=.0001, range_60m=.1)
    decision, provider = await evaluate(eng, snapshot, probability=.9)
    assert len(provider.requests) == 1
    risk = eng.risk.evaluate(decision, snapshot, _context(live_armed=True), FeeQuote(.001, .001, "test"), book)
    assert NET_RR_TOO_LOW in risk.reject_reasons


@pytest.mark.asyncio
async def test_new_observation_does_not_pin_execution_geometry_or_close_a_winner_early(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    positions = []
    for input_profile in ("legacy", SIMPLE_MARKET_INPUT):
        eng = TradingEngine(settings())
        eng.combination = replace(eng.combination, jev_input_profile=input_profile)
        snapshot, book = candidate()
        attach_book(eng, book)
        eng.jev = AssessmentProvider(.43)
        await eng.evaluate_snapshot(snapshot)
        position = next(iter(eng.accounts.accounts["baseline_jev"].positions.values()))
        assert position.max_hold_minutes is None and position.forecast_horizon_minutes is None
        state = eng.states[snapshot.symbol]
        state.best_bid, state.best_ask = 101, 101.02
        await eng.manage_positions(snapshot.symbol, as_of=clock()+timedelta(minutes=61))
        assert str(position.position_id) in eng.accounts.accounts["baseline_jev"].positions
        positions.append(position)
    for attr in ("entry_price", "quantity", "stop", "target", "initial_net_risk", "entry_fee", "exit_fee_rate"):
        assert getattr(positions[0], attr) == getattr(positions[1], attr)


@pytest.mark.asyncio
async def test_time_exit_off_and_loss_circuit_keep_their_existing_meaning():
    eng = TradingEngine(settings())
    eng.risk.update_limits(time_exit_enabled=False)
    snapshot, book = candidate()
    decision, provider = await evaluate(eng, snapshot)
    body = _state_for_model(provider.requests[0].model_dump(mode="json"), .7)
    assert body["trade"]["time_exit"] == {"rule": "TARGET_OR_STOP"}
    risk = eng.risk.evaluate(decision, snapshot, _context(live_armed=True, consecutive_live_losses=10),
                             FeeQuote(.001, .001, "test"), book)
    assert MAX_CONSECUTIVE_LIVE_LOSSES in risk.reject_reasons


@pytest.mark.asyncio
async def test_veto_and_below_threshold_are_not_replaced_by_the_new_payload():
    eng = TradingEngine(settings())
    snapshot, _ = candidate(breakout=False, breakdown=False)
    decision, provider = await evaluate(eng, snapshot, probability=.2)
    assert len(provider.requests) == 1
    assert decision.action is Action.NO_TRADE and "JEV_LOW_CONTINUATION" in decision.reason_codes
    eng.weights = replace(eng.weights, min_abs_score=.95)
    decision, provider = await evaluate(eng, snapshot, probability=.99)
    assert not provider.requests
    assert decision.metadata["jev_effect"] == "idle"


def test_review_exposes_baseline_strength_without_adding_it_to_the_provider_state():
    eng = TradingEngine(settings())
    sent_state = {"candidate_side": "SHORT", "evidence": {"trend": {"ema_alignment": -1}}}
    call = {"decision_id": "test-review", "response": {"sent_request": {"state": sent_state}}}
    decision = {"action": "SHORT", "confidence": .54,
                "metadata": {"composite": -.62, "baseline_action": "SHORT"}}
    review = eng._format_jev_reviews([(call, decision)], 1)[0]
    assert review["baseline_score"] == .62
    assert review["confidence"] == .54
    assert review["state"]["evidence"] == sent_state["evidence"]
    assert "baseline_action" not in sent_state
    assert "baseline_score" not in review["state"]
