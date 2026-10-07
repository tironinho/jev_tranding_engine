from datetime import datetime, timedelta, timezone

from app.config import RiskLimits
from app.domain.enums import NO_STRUCTURE_TARGET, Action
from app.execution.paper import position_exit
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


def test_broken_15m_high_still_has_no_invented_target():
    limits = RiskLimits(target_fallback="none", min_stop_pct=0.0001, max_stop_pct=0.05)
    features = {
        "atr": 1.0,
        "recent_swing_low": 98.0,
        "resistance": 120.0,
        "resistance_15m": 100.0,
    }
    assert plan_geometry(Action.LONG, 101.0, features, limits) == NO_STRUCTURE_TARGET


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
