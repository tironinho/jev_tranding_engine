from datetime import datetime, timedelta, timezone

from app.config import RiskLimits
from app.domain.enums import NO_STRUCTURE_TARGET, Action
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


def test_15m_target_is_used_when_the_1m_high_is_already_broken():
    limits = RiskLimits(target_fallback="none", min_stop_pct=0.0001, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 98.0,
        "resistance": 100.0,
        "resistance_15m": 110.0,
        "support_15m": 90.0,
    }
    geometry = plan_geometry(Action.LONG, 101.0, features, limits)
    assert geometry.target == 110.0


def test_nearby_15m_level_does_not_cap_the_winner_below_the_risk_multiple():
    limits = RiskLimits(target_fallback="rr", rr_target_multiple=2.5, min_stop_pct=0.0001, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 100.0,
        "resistance_15m": 101.2,
        "support_15m": 90.0,
    }
    geometry = plan_geometry(Action.LONG, 101.0, features, limits)
    stop = 100.0 - 0.1
    assert geometry.target == 101.0 + (101.0 - stop) * 2.5


def test_a_stop_tighter_than_the_minimum_is_widened_instead_of_rejected():
    from app.domain.enums import STOP_WIDENED_TO_MIN

    limits = RiskLimits(target_fallback="rr", rr_target_multiple=2.5, min_stop_pct=0.0025, max_stop_pct=0.05)
    features = {"atr": 0.01, "recent_swing_low": 99.99, "resistance_15m": 100.05}
    geometry = plan_geometry(Action.LONG, 100.0, features, limits)
    assert geometry.stop == 100.0 * (1 - 0.0025)
    assert STOP_WIDENED_TO_MIN in geometry.reasons


def test_broken_15m_high_still_has_no_invented_target():
    limits = RiskLimits(target_fallback="none", min_stop_pct=0.0001, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 98.0,
        "resistance": 120.0,
        "resistance_15m": 100.0,
    }
    assert plan_geometry(Action.LONG, 101.0, features, limits) == NO_STRUCTURE_TARGET


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


def test_a_tighter_stop_is_not_loosened():
    position = _position(stop=100.2)
    assert stepped_stop(position, 101.0) is None


def test_time_exit_closes_without_touching_stop_or_target():
    assert position_exit(Action.LONG, bid=100, ask=100.1, stop=95, target=120, hold_minutes=59, max_hold_minutes=60) is None
    assert position_exit(Action.LONG, bid=100, ask=100.1, stop=95, target=120, hold_minutes=60, max_hold_minutes=60) == "TIME"
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
