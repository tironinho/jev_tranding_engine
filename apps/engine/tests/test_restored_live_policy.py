"""Repository defaults must select a15bc12 without restoring execution defects."""
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.domain.enums import Action, NET_RR_TOO_LOW
from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import FIRST_TEN_HISTORICAL_PROMPT, JevProviderError
from app.risk.engine import FeeQuote
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.runners import BaselineJevStrategy
from tests.conftest import clock, long_snapshot, settings
from tests.test_initial_live_profile import AssessmentProvider, context
from tests.test_risk_limits import _context
from tests.test_runtime import _open


def test_repository_defaults_and_status_select_the_historical_policy():
    eng = TradingEngine(settings())
    assert eng.status()["decision_policy"] == {
        "baseline_version": "baseline_first_ten_v1", "baseline_threshold": .45,
        "jev_rule_version": "jev_break_size_v1",
        "request_profile": "live_first_ten_2026_10_09",
        "prompt_version": FIRST_TEN_HISTORICAL_PROMPT,
        "target_rule_version": "live_first_ten_2026_10_09", "min_net_rr": 1.5,
        "cost_aware_target_floor": False, "market_range_plans": False,
        "jev_expectancy_gate": False, "review_below_threshold": False,
        "paper_policy_comparison": False, "max_hold_minutes": 60,
    }
    assert eng.fee_config.maker_fee_rate == eng.fee_config.taker_fee_rate == .001
    assert eng.risk.limits.max_consecutive_live_losses == 3
    assert eng.risk.limits.max_protection_recovery_attempts == 5
    assert not eng.risk.limits.step_stop_to_breakeven


@pytest.mark.asyncio
async def test_small_hour_range_reaches_jev_once_but_still_fails_execution_economics():
    eng = TradingEngine(settings())
    snapshot, book = long_snapshot(atr=.01, range_60m=.1, range_60m_frac=.001)
    assert score_baseline(snapshot, eng.weights).action is Action.LONG
    provider = AssessmentProvider(.9)
    ctx = context(eng, snapshot, provider)
    ctx.round_trip_fee = .002
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, ctx)
    assert len(provider.requests) == 1
    assert provider.requests[0].trade_plan is None
    assert decision.action is Action.LONG
    assert decision.metadata["jev_effect"] == "confirm"
    risk = eng.risk.evaluate(decision, snapshot, _context(live_armed=True), FeeQuote(.001, .001, "test"), book)
    assert NET_RR_TOO_LOW in risk.reject_reasons
    assert risk.economics.net_rr < 1.5


@pytest.mark.asyncio
async def test_default_does_not_send_rejected_baseline_for_shadow_research():
    eng = TradingEngine(settings())
    eng.weights = replace(eng.weights, min_abs_score=.95)
    snapshot, _ = long_snapshot()
    provider = AssessmentProvider(.9)
    decision = await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, provider))
    assert not provider.requests
    assert decision.action is Action.NO_TRADE
    assert decision.metadata["jev_effect"] == "idle"
    assert "BELOW_THRESHOLD" in decision.reason_codes


@pytest.mark.asyncio
async def test_restored_wire_contract_is_compact_and_preserves_raw_answers():
    eng = TradingEngine(settings())
    snapshot, _ = long_snapshot(taker_flow_1m=.3, return_60m=.01, range_60m_frac=.3)
    capture = AssessmentProvider(.43)
    await BaselineJevStrategy().evaluate(snapshot, snapshot.features, context(eng, snapshot, capture))
    def handler(request):
        body = json.loads(request.content)
        assert set(body["state"]) == {
            "symbol", "market_type", "baseline_action", "baseline_confidence",
            "baseline_scores", "baseline_class", "baseline_labels", "taker_flow_1m",
            "return_60m", "range_60m", "breakout", "breakdown",
        }
        assert body["state"]["range_60m"] == .3
        assert {key: value["instructions"] for key, value in body["questions"].items()} == {
            "trend_continuation_probability": "The `baseline_action` side is the candidate from the weighted scores and `baseline_class`. It is not an order. That side reaches the target before the stop.",
            "reversal_probability": "Price reverses against the `baseline_action` candidate.",
            "false_breakout_probability": "The break named in `baseline_class` is a false break. If the class has no break, stay near 0.5.",
        }
        return httpx.Response(200, json={"answers": {name: {"noul": value} for name, value in (
            ("trend_continuation_probability", .43), ("reversal_probability", .31),
            ("false_breakout_probability", .32))}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="default", base_url="https://test.invalid",
            api_key="test-only", model="test", client=client)
        result = await provider.evaluate_market_state(capture.requests[0])
        assert result.trend_continuation_probability == .43
        assert result.reversal_probability == .31
        assert result.false_breakout_probability == .32
        with pytest.raises(JevProviderError, match="historical_contract_does_not_accept_trade_plan"):
            await provider.evaluate_market_state(capture.requests[0].model_copy(update={"trade_plan": {"entry": 100}}))


def test_saved_runtime_cannot_reenable_later_policy_flags():
    eng = TradingEngine(settings())
    before = eng.status()["decision_policy"]
    eng.apply_runtime({"risk": {"cost_aware_target_floor": True, "market_range_plans": True,
        "jev_expectancy_gate": True, "max_hold_minutes": 240, "min_net_rr": 0},
        "combination": {"review_below_threshold": True, "paper_policy_comparison": True},
        "strategies": [{"strategy_key": "baseline_jev", "config": {"market_range_plans": True}}]})
    assert eng.status()["decision_policy"] == before


@pytest.mark.asyncio
async def test_old_position_keeps_its_explicit_240_minute_deadline_on_restart(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = TradingEngine(settings())
    position = _open(eng, opened_at=clock()-timedelta(minutes=61))
    position.max_hold_minutes = position.forecast_horizon_minutes = 240
    restored = TradingEngine(settings())
    restored.apply_runtime({"accounts": eng.accounts.export_state()})
    position = next(iter(restored.accounts.accounts["baseline"].positions.values()))
    state = restored.states[position.symbol]
    state.last_price = state.best_bid = 101
    state.best_ask = 101.02
    await restored._manage_position("baseline", position.symbol, position, state, clock(), None)
    assert str(position.position_id) in restored.accounts.accounts["baseline"].positions
    await restored._manage_position("baseline", position.symbol, position, state,
                                   clock()+timedelta(minutes=180), None)
    assert not restored.accounts.accounts["baseline"].positions
    assert restored.accounts.accounts["baseline"].trades[-1].exit_reason == "TIME"
