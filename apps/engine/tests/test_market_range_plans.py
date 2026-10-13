from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from app.config import RiskLimits
from app.domain.enums import Action, MarketType
from app.providers.jev.real import _normalized_plan
from app.risk.plans import candidate_plans
from app.strategies.runners import BaselineJevStrategy
from app.service import TradingEngine
from tests.conftest import long_snapshot, clock, engine, settings
from tests.test_initial_live_profile import AssessmentProvider, context
from tests.test_runtime import _open


def snapshot(span=2, four_hour_span=3):
    s, book = long_snapshot(range_60m=span, range_60m_frac=span/100,
        range_240m=four_hour_span, return_5m=.002, return_15m=.003,
        return_60m=.004, return_240m=.008, context_15m_slope=.001,
        taker_flow_1m=.4, taker_flow_5m=.3, volume_ratio=1.4,
        atr=.01, recent_swing_low=99.99)
    return s.model_copy(update={"market_type": MarketType.MARGIN}), book


def limits():
    return RiskLimits(market_range_plans=True, cost_aware_target_floor=True,
        min_net_rr=1.5, rr_target_multiple=2.5, max_hold_minutes=240,
        target_rule_version="live_first_ten_2026_10_09")


def test_cost_fitted_target_stays_inside_observed_horizon_range():
    s, _ = snapshot()
    plans = candidate_plans(s, Action.LONG, limits(), .00075)
    assert plans[0]["eligible"]
    assert plans[0]["horizon_minutes"] == 60
    assert plans[0]["net_rr"] >= 1.5-1e-10
    assert abs(plans[0]["target"]-plans[0]["entry"]) <= s.features["range_60m"]
    assert plans[0]["requires_structure_break"] is False


def test_four_hour_plan_requires_aligned_actual_history_and_uses_its_range():
    s, _ = snapshot(.3, 1.5)
    plans = candidate_plans(s, Action.LONG, limits(), .00075)
    assert not plans[0]["eligible"]
    assert plans[0]["reason"] == "MARKET_RANGE_BELOW_COST_TARGET"
    assert plans[1]["eligible"]
    assert plans[1]["horizon_minutes"] == plans[1]["max_hold_minutes"] == 240
    normalized = _normalized_plan(plans[1], s.features, s.features["range_60m_frac"])
    assert normalized["reference_range_minutes"] == 240
    assert normalized["target_range_multiple"] <= 1
    assert plans[1]["interest_estimate_per_unit"] > plans[0]["interest_estimate_per_unit"]
    s.features["return_240m"] = None
    assert len(candidate_plans(s, Action.LONG, limits(), .00075)) == 1


@pytest.mark.asyncio
async def test_range_too_small_does_not_send_an_unreachable_plan_to_jev():
    eng = TradingEngine(settings())
    eng.risk.update_limits(market_range_plans=True, cost_aware_target_floor=True,
        min_net_rr=1.5, max_hold_minutes=240, target_rule_version="live_first_ten_2026_10_09")
    s, _ = snapshot(.1, .2)
    provider = AssessmentProvider(.9)
    result = await BaselineJevStrategy().evaluate(s, s.features, context(eng, s, provider))
    assert result.action is Action.NO_TRADE
    assert "MARKET_RANGE_BELOW_COST_TARGET" in result.reason_codes
    assert not provider.requests


@pytest.mark.asyncio
async def test_explicit_plan_horizon_closes_a_winner_on_the_clock(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine()
    p = _open(eng, mode="live", stop_client_order_id="stop", opened_at=clock()-timedelta(minutes=61))
    p.protection_status = "PROTECTED"
    p.max_hold_minutes = p.forecast_horizon_minutes = 60
    state = eng.states[p.symbol]
    state.last_price = 101
    state.best_bid, state.best_ask = 101, 101.02
    eng.risk.update_limits(time_exit_enabled=True)
    eng.live.fetch_verified = AsyncMock(return_value={"status": "NEW"})
    eng._exit_live = AsyncMock()
    await eng._manage_position("baseline", p.symbol, p, state, clock(), None)
    assert eng._exit_live.call_args.args[3] == "TIME"



def test_four_hour_feature_excludes_future_candles_and_rejects_gaps():
    from app.features.engine import compute_features
    from app.market.state import SymbolMarketState, Candle
    state = SymbolMarketState(symbol="BTCUSDT", market_type=MarketType.MARGIN)
    candles = [Candle(open_time=clock()+timedelta(minutes=i),
        close_time=clock()+timedelta(minutes=i+1)-timedelta(milliseconds=1),
        open=100+i*.01, high=100.02+i*.01, low=99.98+i*.01,
        close=100+i*.01, volume=10, closed=True, timeframe="1m") for i in range(241)]
    state.candles["1m"] = candles
    as_of = candles[-1].close_time
    before, _ = compute_features(state, as_of, candles[-1].close)
    assert before["range_240m"] is not None
    state.candles["1m"].append(Candle(open_time=clock()+timedelta(minutes=241),
        close_time=clock()+timedelta(minutes=242), open=9999, high=9999, low=1,
        close=9999, volume=99999, closed=True, timeframe="1m"))
    after, _ = compute_features(state, as_of, candles[-2].close)
    assert after["range_240m"] == before["range_240m"]
    state.candles["1m"].pop(100)
    missing, _ = compute_features(state, as_of, 100)
    assert missing["range_240m"] is None



@pytest.mark.asyncio
async def test_four_hour_paper_entry_keeps_assessed_target_and_persisted_deadline():
    from tests.conftest import attach_book
    from app.domain.schemas import PositionRecord
    eng = TradingEngine(settings(initial_paper_equity=20))
    s, book = snapshot(.3, 1.5)
    attach_book(eng, book)
    provider = AssessmentProvider(.8)
    eng.jev = provider
    await eng.evaluate_snapshot(s)
    plan = provider.requests[0].trade_plan
    assert plan["horizon_minutes"] == 240
    p = next(iter(eng.accounts.accounts["baseline_jev"].positions.values()))
    assert p.target == plan["target"]
    assert p.stop == plan["stop"]
    restored = PositionRecord.model_validate(p.model_dump(mode="json"))
    assert restored.max_hold_minutes == restored.forecast_horizon_minutes == 240
