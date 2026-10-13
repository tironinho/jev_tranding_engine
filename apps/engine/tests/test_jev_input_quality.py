import json
from dataclasses import replace

import httpx
import pytest

from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import JevMarketRequest
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from tests.conftest import clock, long_snapshot, settings

PROFILE = "live_first_ten_2026_10_09"


@pytest.mark.parametrize("flow", [-.8, .8])
@pytest.mark.parametrize("activity", [.2, .6, .99, 1.0])
def test_production_baseline_weak_volume_is_neutral_for_both_flow_directions(flow, activity):
    eng = TradingEngine(settings())
    snapshot, _ = long_snapshot(volume_ratio=activity,taker_flow_1m=flow,
                                 orderflow_delta_ratio=-flow)
    result = score_baseline(snapshot,eng.weights)
    assert eng.weights.version == "baseline_first_ten_v2"
    assert result.scores["volume_score"] == 0
    assert result.scores["orderflow_score"]*flow > 0
    assert eng.weights.min_abs_score == .45


@pytest.mark.parametrize("flow", [-.8,.8])
def test_strong_activity_and_missing_flow_keep_their_meaning_in_production(flow):
    eng = TradingEngine(settings())
    snapshot, _ = long_snapshot(volume_ratio=2.5,taker_flow_1m=flow)
    strong = score_baseline(snapshot,eng.weights)
    assert strong.scores["volume_score"]*flow > 0
    missing = snapshot.model_copy(update={"features":{**snapshot.features,
        "taker_flow_1m":None,"orderflow_delta_ratio":None}})
    unknown = score_baseline(missing,eng.weights)
    assert unknown.scores["volume_score"] is None
    assert unknown.scores["orderflow_score"] is None


def test_old_baseline_can_still_replay_the_historical_volume_vote():
    eng = TradingEngine(settings())
    snapshot, _ = long_snapshot(volume_ratio=.6,taker_flow_1m=-.8)
    before = score_baseline(snapshot,replace(eng.weights,version="baseline_first_ten_v1"))
    after = score_baseline(snapshot,eng.weights)
    assert before.scores["volume_score"] > 0
    assert after.scores["volume_score"] == 0
    assert after.scores["orderflow_score"] == before.scores["orderflow_score"]


async def evaluate(features, labels=None, intelligence=None):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={"answers":{name:{"noul":p} for name,p in (
            ("trend_continuation_probability",.41),("reversal_probability",.32),
            ("false_breakout_probability",.33))}})
    request = JevMarketRequest(prompt_version="jev_input_quality_test",evaluation_profile=PROFILE,
        symbol="SOLUSDT",market_type="margin",timestamp=clock(),features=features,
        baseline_action="SHORT",baseline_class="CLASS_ALIGNED",baseline_labels=labels or ["CLASS_ALIGNED"],
        intelligence=intelligence,trade_plan={"entry":100,"stop":100.3,"target":99,
            "horizon_minutes":240,"net_rr":1.5})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RealJevProvider(prompt_version="test",base_url="https://test.invalid",
            api_key="test-only",model="test",client=client)
        assessment = await provider.evaluate_market_state(request)
    # Prompt cleanup must not transform the provider's answers into confirmations.
    assert assessment.trend_continuation_probability == .41
    assert assessment.reversal_probability == .32
    assert assessment.false_breakout_probability == .33
    assert assessment.sent_request == bodies[0]
    return bodies[0]


@pytest.mark.parametrize("intelligence", [None,
    {"data_quality":{"overall":.2},"onchain":{"support_long":.8,"evidence":1}},
    {"data_quality":{"overall":.9},"binance_agent":{"quality":1,"state":"BEARISH"}}])
@pytest.mark.asyncio
async def test_absent_or_filtered_evidence_does_not_leave_instructions(intelligence):
    body = await evaluate({"breakout":False,"breakdown":True},intelligence=intelligence)
    for question in body["questions"].values():
        assert "onchain" not in question["instructions"]
        assert "binance_agent" not in question["instructions"]
    plan_question = body["questions"]["trend_continuation_probability"]["instructions"]
    assert "within horizon_minutes" in plan_question
    assert "do not raise probability to meet an entry threshold" in plan_question
    assert body["state"]["trade_plan"]["horizon_minutes"] == 240


@pytest.mark.asyncio
async def test_present_onchain_evidence_keeps_its_side_and_explanation():
    body = await evaluate({"breakdown":True},intelligence={"data_quality":{"overall":.9},
        "onchain":{"support_long":.8,"evidence":1}})
    assert body["state"]["intelligence"]["onchain"]["support"] == -.8
    assert body["state"]["intelligence"]["onchain"]["class"] == "HOSTILE"
    assert all("onchain.support" in q["instructions"] for q in body["questions"].values())


@pytest.mark.parametrize("features,labels,event", [
    ({"breakout":False,"breakdown":True},["CLASS_ALIGNED","BREAKDOWN"],"BREAKDOWN"),
    ({"breakout":True,"breakdown":False},["CLASS_ALIGNED","BREAKOUT"],"BREAKOUT"),
    ({},["CLASS_ALIGNED","BREAKDOWN"],"BREAKDOWN"),
    ({"breakout":False,"breakdown":False},["CLASS_ALIGNED"],"NONE"),
    ({},["CLASS_ALIGNED"],"UNKNOWN"),
    ({"breakout":True,"breakdown":True},["CLASS_ALIGNED"],"UNKNOWN"),
])
@pytest.mark.asyncio
async def test_break_question_uses_the_event_even_when_primary_class_is_alignment(features,labels,event):
    body = await evaluate(features,labels)
    assert body["state"]["baseline_class"] == "CLASS_ALIGNED"
    assert body["state"]["break_event"] == event
    question = body["questions"]["false_breakout_probability"]["instructions"]
    assert "break_event" in question
    assert "NONE or UNKNOWN" in question
