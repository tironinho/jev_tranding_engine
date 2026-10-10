from app.domain.enums import (
    BAD_SPREAD,
    BELOW_THRESHOLD,
    CLASS_ALIGNED,
    CLASS_DIVERGENT,
    CLASS_FLOW_AGAINST,
    CLASS_SINGLE_DRIVER,
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


def test_missing_spread_and_volume_do_not_wipe_the_candle_score():
    eng = engine()
    snapshot, _book = long_snapshot(spread_bps=None, volume_ratio=None)
    result = score_baseline(snapshot, eng.weights)
    assert "INSUFFICIENT_HISTORY" not in result.reason_codes
    assert result.scores["trend_score"] is not None


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


def test_below_average_volume_withholds_instead_of_reversing_its_vote():
    eng = engine()
    snapshot, _book = long_snapshot(volume_ratio=.6, taker_flow_1m=.8)

    result = score_baseline(snapshot, eng.weights)

    assert result.scores["volume_score"] == 0
    assert result.scores["orderflow_score"] > 0


def test_closed_multi_timeframe_context_changes_trend_score():
    eng = engine()
    neutral, _book = long_snapshot()
    bearish = neutral.model_copy(update={"features": {
        **neutral.features,
        "context_15m_slope": -.005,
        "price_vs_ema20_15m": -.01,
        "setup_5m_return": -.005,
    }})
    bullish = neutral.model_copy(update={"features": {
        **neutral.features,
        "context_15m_slope": .005,
        "price_vs_ema20_15m": .01,
        "setup_5m_return": .005,
    }})

    bearish_result = score_baseline(bearish, eng.weights)
    bullish_result = score_baseline(bullish, eng.weights)

    assert bearish_result.scores["context_trend_score"] == -1
    assert bullish_result.scores["context_trend_score"] == 1
    assert bearish_result.scores["trend_score"] < bullish_result.scores["trend_score"]


def test_confirmed_breakdown_overrides_opposite_lagging_context():
    eng = engine()
    snapshot, _book = long_snapshot(
        breakdown=True,
        breakout=False,
        rsi=35,
        roc=-.004,
        taker_flow_1m=-.8,
        imbalance_10=-.4,
        context_15m_slope=.005,
        price_vs_ema20_15m=.01,
        setup_5m_return=.005,
    )

    result = score_baseline(snapshot, eng.weights)

    assert result.scores["context_trend_score"] > 0
    assert result.scores["break_confirmation_score"] == -1
    assert result.scores["trend_score"] <= 0


def test_aligned_components_are_classified_before_the_side():
    eng = engine()
    snapshot, _book = long_snapshot()
    result = score_baseline(snapshot, eng.weights)
    assert result.market_class.primary == CLASS_ALIGNED
    assert CLASS_ALIGNED in result.reason_codes
    assert result.action is Action.LONG


def test_one_component_does_not_become_a_trade():
    eng = engine()
    snapshot, _book = long_snapshot(
        rsi=50,
        roc=0,
        volume_ratio=1,
        orderflow_delta_ratio=0,
        imbalance_10=0,
        range_position=0.5,
        breakout=False,
        breakdown=False,
    )
    result = score_baseline(snapshot, eng.weights)
    assert result.market_class.primary == CLASS_SINGLE_DRIVER
    assert result.action is Action.NO_TRADE
    assert CLASS_SINGLE_DRIVER in result.reason_codes


def test_flow_against_the_composite_refuses_the_side():
    eng = engine()
    snapshot, _book = long_snapshot(
        volume_ratio=1,
        orderflow_delta_ratio=-0.25,
        imbalance_10=0,
        taker_flow_1m=None,
    )
    result = score_baseline(snapshot, eng.weights)
    assert result.composite > eng.weights.min_abs_score
    assert CLASS_FLOW_AGAINST in result.market_class.labels
    assert result.action is Action.NO_TRADE


def test_opposing_components_are_divergent_and_do_not_trade():
    eng = engine()
    snapshot, _book = long_snapshot(
        rsi=30,
        roc=-0.004,
        volume_ratio=1.8,
        orderflow_delta_ratio=-0.8,
        imbalance_10=-0.4,
        range_position=0.5,
        breakout=False,
        breakdown=False,
    )
    result = score_baseline(snapshot, eng.weights)
    assert CLASS_DIVERGENT in result.market_class.labels
    assert result.action is Action.NO_TRADE
