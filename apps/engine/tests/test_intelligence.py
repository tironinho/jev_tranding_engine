import math
from datetime import datetime, timedelta, timezone

import pytest

from app.intelligence.binance_agent import build_binance_agent_context
from app.intelligence.asof import asof_backward
from app.intelligence.freshness import age_seconds, freshness
from app.intelligence.normalize import (
    book_imbalance,
    fear_greed_signed,
    funding_bps,
    liquidation_imbalance,
    log_ratio,
    oi_log_change,
    robust_zscore,
    signed_from_z,
    spread_bps,
    taker_imbalance,
)
from app.intelligence.protocol import DisabledProvider
from app.intelligence.quality import feature_quality, missing_feature, relative_disagreement
from app.intelligence.schemas import RawExternalObservation


def test_fear_greed_signed_is_centered_and_clamped():
    assert fear_greed_signed(0) == -1.0
    assert fear_greed_signed(50) == 0.0
    assert fear_greed_signed(100) == 1.0
    assert fear_greed_signed(120) == 1.0
    assert fear_greed_signed(None) is None


def test_robust_zscore_uses_median_and_rejects_a_zero_mad_outlier():
    history = [1.0, 1.0, 1.0, 1.0, 3.0]
    assert robust_zscore(1.0, history) == 0.0
    assert robust_zscore(100.0, [1.0, 1.0, 1.0]) is None
    score = robust_zscore(10.0, [0.0, 1.0, 2.0, 3.0, 4.0])
    assert score is not None and math.isfinite(score)
    signed = signed_from_z(1e9)
    assert signed is not None and math.isfinite(signed)
    assert -1.0 <= signed <= 1.0
    assert signed_from_z(float("inf")) is None
    assert signed_from_z(float("nan")) is None


def test_oi_funding_ratio_and_flow_formulas():
    assert oi_log_change(math.e, 1.0) == pytest.approx(1.0)
    assert oi_log_change(None, 1.0) is None
    assert oi_log_change(0.0, 1.0) is None
    assert funding_bps(0.0001) == pytest.approx(1.0)
    assert log_ratio(math.e) == pytest.approx(1.0)
    assert log_ratio(0.0) is None
    assert liquidation_imbalance(0.0, 10.0) == pytest.approx(1.0, abs=1e-9)
    assert liquidation_imbalance(10.0, 0.0) == pytest.approx(-1.0, abs=1e-9)
    assert liquidation_imbalance(None, 1.0) is None
    assert taker_imbalance(3.0, 1.0) == pytest.approx(0.5)
    assert taker_imbalance(None, 1.0) is None
    assert book_imbalance(3.0, 1.0) == pytest.approx(0.5)
    assert book_imbalance(0.0, 0.0) is None
    assert spread_bps(100.0, 100.1) == pytest.approx((0.1 / 100.05) * 10_000)


def test_freshness_decays_and_rejects_the_future():
    assert freshness(0, 10) == 1.0
    assert freshness(10, 10) == pytest.approx(math.exp(-1))
    assert freshness(-1, 10) is None
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert age_seconds(now, now - timedelta(seconds=5)) == 5


def test_missing_is_not_zero_and_disagreement_lowers_quality():
    missing = missing_feature()
    assert missing["value"] is None
    assert missing["available"] is False
    assert feature_quality(available=False, fresh=1.0) == 0.0
    gap = relative_disagreement(100.0, 50.0)
    assert gap == pytest.approx(0.5)
    high = feature_quality(available=True, fresh=1.0, disagreement=0.0)
    low = feature_quality(available=True, fresh=1.0, disagreement=0.5)
    assert low < high
    assert relative_disagreement(None, 1.0) is None


def test_asof_ignores_a_print_one_second_after_the_decision():
    decision = datetime(2026, 10, 8, 10, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"observed_at": decision - timedelta(minutes=1), "value": 1.0},
        {"observed_at": decision + timedelta(seconds=1), "value": 99.0},
    ]
    chosen = asof_backward(rows, decision, lambda row: row["observed_at"])
    assert chosen["value"] == 1.0
    assert asof_backward([rows[1]], decision, lambda row: row["observed_at"]) is None


@pytest.mark.asyncio
async def test_a_disabled_provider_does_not_raise():
    provider = DisabledProvider("cryptoquant")
    health = await provider.health()
    assert health.status == "DISABLED"
    assert await provider.collect(["BTCUSDT"], datetime.now(timezone.utc)) == []


def test_raw_observation_keeps_a_null_value():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    row = RawExternalObservation(
        provider="alternative_me",
        metric="fear_greed",
        value=None,
        unit="index",
        observed_at=now,
        received_at=now,
    )
    assert row.value is None


def test_binance_agent_is_read_only_and_summarizes_exchange_features():
    context = build_binance_agent_context(
        {
            "ema_alignment": 1.0,
            "return_15m": 0.01,
            "context_15m_slope": 0.005,
            "price_vs_ema20_15m": 0.01,
            "roc": 0.004,
            "return_5m": 0.005,
            "rsi": 65,
            "taker_flow_1m": 0.6,
            "taker_flow_5m": 0.4,
            "orderflow_delta_ratio": 0.5,
            "imbalance_5": 0.4,
            "imbalance_10": 0.3,
            "imbalance_20": 0.2,
            "spread_bps": 1.0,
            "volume_ratio": 1.4,
            "atr_normalized": 0.005,
        }
    )
    assert context["mode"] == "READ_ONLY_SHADOW"
    assert context["can_execute"] is False
    assert context["state"] == "BULLISH"
    assert context["directional_bias"] > 0
    assert context["quality"] == 1.0


def test_binance_agent_withholds_a_bias_when_data_is_thin():
    context = build_binance_agent_context({"spread_bps": 12.0})
    assert context["state"] == "INSUFFICIENT_DATA"
    assert context["risk_flags"] == ["WIDE_SPREAD"]
