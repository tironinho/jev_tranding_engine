from datetime import datetime, timedelta, timezone

from app.config import RiskLimits
from app.domain.enums import Action
from types import SimpleNamespace

from app.execution.paper import position_exit, stepped_stop
from app.features.engine import compute_features
from app.market.state import Candle, SymbolMarketState
from app.risk.economics import plan_geometry


def _fifteen(index: int, high: float, low: float) -> Candle:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=15) - timedelta(milliseconds=1),
        open=low,
        high=high,
        low=low,
        close=(high + low) / 2,
        volume=10,
        closed=True,
        timeframe="15m",
    )


def test_a_wide_hour_leaves_the_target_at_risk_multiple():
    limits = RiskLimits(cap_target_by_rr=True, rr_target_multiple=2.5, min_stop_pct=0.0001, atr_stop_mult=0.01, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 100.0,
        "range_60m": 20.0,
    }
    geometry = plan_geometry(Action.LONG, 101.0, features, limits)
    stop = 100.0 - 0.1
    assert (101.0 - stop) * 2.5 < 20.0
    assert geometry.target == 101.0 + (101.0 - stop) * 2.5


def test_the_target_shrinks_to_the_last_hour():
    limits = RiskLimits(cap_target_by_rr=True, rr_target_multiple=2.5, min_stop_pct=0.0001, atr_stop_mult=0.01, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 100.0,
        "range_60m": 1.5,
    }
    geometry = plan_geometry(Action.LONG, 101.0, features, limits)
    assert geometry.target == 101.0 + 1.5


def test_a_missing_hour_has_no_target():
    from app.domain.enums import HOUR_RANGE_UNAVAILABLE

    limits = RiskLimits(min_stop_pct=0.0001, max_stop_pct=0.05)
    features = {"atr": 1.0, "recent_swing_low": 98.0, "resistance_15m": 110.0}
    assert plan_geometry(Action.LONG, 101.0, features, limits) == HOUR_RANGE_UNAVAILABLE


def test_an_atr_wider_than_the_fee_floor_sets_the_stop():
    limits = RiskLimits(target_fallback="rr", rr_target_multiple=2.5, min_stop_pct=0.0025, atr_stop_mult=2.0, max_stop_pct=0.05)
    features = {"atr": 1.0, "recent_swing_low": 99.9, "range_60m": 10.0}
    geometry = plan_geometry(Action.LONG, 100.0, features, limits)
    assert geometry.stop == 98.0


def test_a_stop_tighter_than_the_minimum_is_widened_instead_of_rejected():
    from app.domain.enums import STOP_WIDENED_TO_MIN

    limits = RiskLimits(target_fallback="rr", rr_target_multiple=2.5, min_stop_pct=0.0025, max_stop_pct=0.05)
    features = {"atr": 0.01, "recent_swing_low": 99.99, "range_60m": 2.0}
    geometry = plan_geometry(Action.LONG, 100.0, features, limits)
    assert geometry.stop == 100.0 * (1 - 0.0025)
    assert STOP_WIDENED_TO_MIN in geometry.reasons


def test_a_one_minute_bar_wider_than_the_fee_floor_sets_the_stop():
    limits = RiskLimits(cap_target_by_rr=True, rr_target_multiple=2.5, min_stop_pct=0.0025, atr_stop_mult=2.0, max_stop_pct=0.05)
    features = {"atr": 0.01, "recent_swing_low": 99.99, "range_1m": 0.8, "range_60m": 3.0}
    geometry = plan_geometry(Action.LONG, 100.0, features, limits)
    assert geometry.stop == 99.2
    assert geometry.target == 100.0 + min(0.8 * 2.5, 3.0)


def _position(**overrides):
    values = dict(
        side=Action.LONG,
        entry_price=100.0,
        stop=99.0,
        initial_stop=99.0,
        target=102.5,
        quantity=1.0,
        entry_fee=0.1,
        exit_fee_rate=0.0005,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cusum_ignores_noise_and_fires_on_an_atr_move():
    from app.market.cusum import CusumFilter

    filt = CusumFilter(1.0)
    assert filt.event("BTCUSDT", 100.0, 1.0) is False
    assert filt.event("BTCUSDT", 100.2, 1.0) is False
    assert filt.event("BTCUSDT", 101.3, 1.0) is True
    assert filt.event("BTCUSDT", 101.4, 1.0) is False


def test_continuation_cut_stays_at_least_the_configured_floor():
    from types import SimpleNamespace

    from app.config import CombinationConfig, RiskLimits
    from app.strategies.runners import _continuation_floor

    snapshot = SimpleNamespace(price=100.0, features={"atr": 0.036})
    context = SimpleNamespace(
        risk=RiskLimits(),
        combination=CombinationConfig(),
        round_trip_fee=0.001,
    )
    assert _continuation_floor(snapshot, context, Action.LONG) == 0.55
    assert _continuation_floor(snapshot, context, Action.SHORT) == 0.40


def test_meta_hit_probability_rises_when_the_stop_is_tight():
    from app.strategies.rules import meta_hit_probability

    wide = meta_hit_probability(2.5, 0.001, 0.01)
    tight = meta_hit_probability(2.5, 0.001, 0.0025)
    assert wide < tight
    assert tight == (1 + 0.001 / 0.0025) / 3.5


def test_stop_stays_put_inside_the_first_r():
    assert stepped_stop(_position(), 100.4) is None


def test_one_r_moves_a_long_stop_to_fee_breakeven():
    locked = stepped_stop(_position(), 101.0)
    assert locked == (100.0 + 0.1) / (1 - 0.0005)


def test_two_r_locks_one_r_of_profit():
    assert stepped_stop(_position(), 102.0) == 101.0


def test_short_mirrors_the_same_steps():
    short = _position(side=Action.SHORT, stop=101.0, initial_stop=101.0, target=97.5)
    assert stepped_stop(short, 99.5) is None
    assert stepped_stop(short, 99.0) == (100.0 - 0.1) / (1 + 0.0005)
    assert stepped_stop(short, 98.0) == 99.0


def test_max_hold_closes_only_flat_or_losing_positions():
    assert position_exit(Action.LONG, bid=100, ask=100.1, stop=95, target=120, hold_minutes=59, max_hold_minutes=60) is None
    assert position_exit(Action.LONG, bid=100, ask=100.1, stop=95, target=120, hold_minutes=60, max_hold_minutes=60, entry=100) == "TIME"
    assert position_exit(Action.LONG, bid=101, ask=101.1, stop=95, target=120, hold_minutes=60, max_hold_minutes=60, entry=100) is None
    assert position_exit(Action.SHORT, bid=99, ask=99.4, stop=105, target=90, hold_minutes=60, max_hold_minutes=60, entry=100) is None
    assert position_exit(Action.SHORT, bid=100.2, ask=100.3, stop=105, target=90, hold_minutes=60, max_hold_minutes=60, entry=100) == "TIME"
    assert position_exit(Action.LONG, bid=94, ask=94.1, stop=95, target=120, hold_minutes=90, max_hold_minutes=60) == "STOP"


def test_15m_resistance_ignores_the_latest_closed_bar_and_the_future():
    state = SymbolMarketState("BTCUSDT", "spot")
    candles = [_fifteen(i, high=100 + i, low=90) for i in range(25)]
    state.candles["15m"] = candles
    as_of = candles[22].close_time
    features, _quality = compute_features(state, as_of, 110)
    # Bars available through index 22. Excluding the latest leaves highs 100..121, max of last 20 of those.
    assert features["resistance_15m"] == max(100 + i for i in range(2, 22))
    candles.append(_fifteen(30, high=500, low=90))
    state.candles["15m"] = candles
    after, _ = compute_features(state, as_of, 110)
    assert after["resistance_15m"] == features["resistance_15m"]
