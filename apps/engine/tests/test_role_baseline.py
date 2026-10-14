from datetime import timedelta, datetime, timezone

from app.domain.enums import Action
from app.features.engine import compute_features, five_minute_structure
from app.market.state import Candle, SymbolMarketState
from app.service import TradingEngine
from app.strategies.baseline_score import score_baseline
from app.strategies.role_baseline import assess_roles
from tests.conftest import long_snapshot, settings


def viable(**overrides):
    features = dict(
        context_15m_slope=.002, price_vs_ema20_15m=.001, distance_to_vwap=.001,
        breakout_5m=True, breakdown_5m=False, bullish_reclaim_5m=False, bearish_reclaim_5m=False,
        retest_long_5m=False, retest_short_5m=False,
        extension_above_resistance_5m_atr=.2, extension_below_support_5m_atr=2,
        taker_flow_1m=.4, taker_flow_5m=.3, volume_ratio=1.2,
        spread_bps=2, atr=.4, bid_liquidity_10=10, ask_liquidity_10=8,
        rsi=73, roc=.01, return_1m=.001, return_5m=.004, breakout=True, breakdown=False,
    )
    features.update(overrides)
    return features


def test_continuation_keeps_context_structure_and_flow_in_separate_roles():
    result = assess_roles(Action.LONG, viable())
    assert result["setup"] == "CONTINUATION" and result["eligible"] and result["structure_event"] == "BREAK"
    assert result["can_authorize"] is False
    assert "rsi" not in result["reasons"]
    quiet = assess_roles(Action.LONG, viable(volume_ratio=.4))
    loud = assess_roles(Action.LONG, viable(volume_ratio=2))
    assert quiet["setup"] == loud["setup"] == "CONTINUATION"
    assert loud["rank"] > quiet["rank"]


def test_one_minute_impulse_against_15m_context_is_not_a_continuation():
    features = viable(context_15m_slope=-.003, price_vs_ema20_15m=-.004, distance_to_vwap=-.002,
                      breakout_5m=False, breakout=True)
    result = assess_roles(Action.LONG, features)
    assert result["setup"] == "REVERSAL"
    assert not result["eligible"]
    assert "REVERSAL_RECLAIM_REQUIRED" in result["reasons"]


def test_reversal_needs_a_5m_reclaim_and_flow_on_both_windows():
    base = viable(context_15m_slope=-.003, price_vs_ema20_15m=-.004, distance_to_vwap=.001,
                  breakout_5m=False, bullish_reclaim_5m=True)
    assert assess_roles(Action.LONG, base)["eligible"]
    impulse = dict(base, taker_flow_5m=-.2)
    blocked = assess_roles(Action.LONG, impulse)
    assert not blocked["eligible"]
    assert "IMPULSE_WITHOUT_SUSTAINED_FLOW" in blocked["reasons"]


def test_partial_context_disagreement_is_not_an_automatic_veto():
    majority = viable(distance_to_vwap=-.002)
    assert assess_roles(Action.LONG, majority)["context"] == "ALIGNED"
    assert assess_roles(Action.LONG, majority)["eligible"]
    minority = viable(context_15m_slope=-.003, price_vs_ema20_15m=-.004, distance_to_vwap=.001,
                      breakout_5m=False, bullish_reclaim_5m=True)
    recovered = assess_roles(Action.SHORT, {**minority, "bearish_reclaim_5m": True, "bullish_reclaim_5m": False,
                                            "taker_flow_1m": -.4, "taker_flow_5m": -.3,
                                            "context_15m_slope": .003, "price_vs_ema20_15m": -.004,
                                            "distance_to_vwap": 0})
    assert recovered["context"] == "MIXED" and recovered["setup"] == "REVERSAL" and recovered["eligible"]


def test_a_wide_spread_is_not_offset_by_rsi():
    result = assess_roles(Action.LONG, viable(spread_bps=20, rsi=80))
    assert not result["eligible"]
    assert "SPREAD_FILTER" in result["filters"]


def test_missing_context_is_not_treated_as_agreement():
    features = viable()
    for key in ("context_15m_slope", "price_vs_ema20_15m", "distance_to_vwap"):
        features.pop(key)
    result = assess_roles(Action.LONG, features)
    assert result["context"] == "UNKNOWN" and not result["eligible"]


def test_extended_entry_ranks_below_the_same_setup_near_the_level():
    near = assess_roles(Action.LONG, viable(extension_above_resistance_5m_atr=.2))
    far = assess_roles(Action.LONG, viable(extension_above_resistance_5m_atr=2))
    assert near["eligible"] and far["eligible"] and far["extended"]
    assert near["rank"] > far["rank"]


def test_active_baseline_floor_and_score_stay_in_place():
    eng = TradingEngine(settings())
    assert eng.weights.min_abs_score == 0.45
    snapshot, _book = long_snapshot(**viable(
        context_15m_slope=-.003, price_vs_ema20_15m=-.004, distance_to_vwap=-.002, breakout_5m=False))
    result = score_baseline(snapshot, eng.weights)
    role = assess_roles(Action.LONG, snapshot.features)
    assert result.action is Action.LONG
    assert not role["eligible"] and role["can_authorize"] is False


def _bar(index, close, *, closed=True):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index)
    return Candle(start, start + timedelta(minutes=5) - timedelta(milliseconds=1),
                  close, close, close, close, 1, closed, "5m")


def test_five_minute_structure_ignores_a_bar_that_has_not_closed():
    quiet = [_bar(i, 100) for i in range(20)]
    broke = _bar(20, 110)
    assert five_minute_structure([*quiet, broke], 1)["breakout_5m"] is True
    state = SymbolMarketState(symbol="BTCUSDT", market_type="spot")
    state.candles["1m"] = []
    state.candles["5m"] = [*quiet, broke, _bar(21, 80, closed=False), _bar(22, 80)]
    features, _quality = compute_features(state, broke.close_time, broke.close)
    assert features["breakout_5m"] is True
    assert features["breakdown_5m"] is False
