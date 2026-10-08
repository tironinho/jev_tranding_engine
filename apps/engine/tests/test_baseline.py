from app.domain.enums import (
    BAD_SPREAD,
    BELOW_THRESHOLD,
    STALE_MARKET_DATA,
    Action,
)
from app.strategies.baseline_score import score_baseline
from tests.conftest import engine, long_snapshot


def test_baseline_is_deterministic_and_has_no_size():
    eng = engine()
    snapshot, _book = long_snapshot()
    first = score_baseline(snapshot, eng.weights)
    second = score_baseline(snapshot, eng.weights)
    assert first == second
    assert first.action is Action.LONG
    assert "TREND_UP" in first.reason_codes
    dumped = first.__dict__
    assert "quantity" not in dumped
    assert "leverage" not in dumped


def test_stale_market_is_no_trade():
    eng = engine()
    snapshot, _book = long_snapshot()
    stale = snapshot.model_copy(update={"data_quality": snapshot.data_quality.model_copy(update={"stale": True})})
    result = score_baseline(stale, eng.weights)
    assert result.action is Action.NO_TRADE
    assert result.reason_codes == [STALE_MARKET_DATA]


def test_wide_spread_blocks():
    eng = engine()
    snapshot, _book = long_snapshot(spread_bps=25)
    result = score_baseline(snapshot, eng.weights)
    assert result.action is Action.NO_TRADE
    assert BAD_SPREAD in result.reason_codes


def test_flat_scores_do_not_trade():
    eng = engine()
    snapshot, _book = long_snapshot(
        ema_alignment=0,
        ema_20_slope=0,
        price_vs_ema20=0,
        rsi=50,
        roc=0,
        volume_ratio=1,
        orderflow_delta_ratio=0,
        imbalance_10=0,
        range_position=0.5,
        breakout=False,
    )
    result = score_baseline(snapshot, eng.weights)
    assert result.action is Action.NO_TRADE
    assert BELOW_THRESHOLD in result.reason_codes


def test_missing_flow_does_not_push_volume_long():
    eng = engine()
    snapshot, _book = long_snapshot(
        ema_alignment=0,
        ema_20_slope=0,
        price_vs_ema20=0,
        rsi=50,
        roc=0,
        volume_ratio=2.5,
        orderflow_delta_ratio=None,
        taker_flow_1m=None,
        imbalance_10=0,
        range_position=0.5,
        breakout=False,
        breakdown=False,
    )
    result = score_baseline(snapshot, eng.weights)
    assert result.scores["volume_score"] is None
    assert result.scores["orderflow_score"] is None
    assert result.action is not Action.LONG


def test_taker_flow_is_used_before_the_aggtrade_delta():
    eng = engine()
    snapshot, _book = long_snapshot(taker_flow_1m=-0.8, orderflow_delta_ratio=0.9, volume_ratio=1.5)
    result = score_baseline(snapshot, eng.weights)
    assert result.scores["orderflow_score"] is not None
    assert result.scores["orderflow_score"] < 0
    assert result.scores["volume_score"] is not None
    assert result.scores["volume_score"] < 0
