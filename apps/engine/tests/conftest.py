from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.config import BaselineWeightConfig, Settings
from app.domain.enums import Action, MarketType, OperatingMode
from app.domain.schemas import BookLevel, DataQuality, MarketSnapshot
from app.execution.slippage import book_from_levels
from app.service import TradingEngine


def settings(**overrides) -> Settings:
    data = dict(
        _env_file=None,
        database_url="",
        environment="development",
        trading_engine_enabled=True,
        trading_live_enabled=False,
        allow_real_orders=False,
        binance_api_key="",
        binance_api_secret="",
        openai_api_key="",
        jev_provider="mock",
        jev_api_key="",
        jev_base_url="",
        market_type="futures",
        symbols="BTCUSDT",
        initial_paper_equity=10_000,
        maker_fee_rate=0.0005,
        taker_fee_rate=0.0005,
        max_symbol_exposure=1,
        max_total_exposure=1,
        stale_after_ms=5_000,
        paper_seed=1,
    )
    data.update(overrides)
    return Settings(**data)


def engine(**overrides) -> TradingEngine:
    return TradingEngine(settings(**overrides))


def clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def long_snapshot(**feature_overrides) -> MarketSnapshot:
    features = {
        "ema_alignment": 1.0,
        "ema_20_slope": 0.001,
        "price_vs_ema20": 0.002,
        "rsi": 64.0,
        "roc": 0.003,
        "volume_ratio": 1.4,
        "volume_zscore": 1.2,
        "orderflow_delta_ratio": 0.45,
        "imbalance_10": 0.3,
        "range_position": 0.82,
        "breakout": True,
        "breakdown": False,
        "atr_normalized": 0.004,
        "atr": 0.4,
        "spread_bps": 2.0,
        "distance_to_vwap": 0.001,
        "cvd_slope": 4.0,
        "recent_swing_low": 98.6,
        "recent_swing_high": 101.0,
        "support": 97.0,
        "resistance": 108.0,
        "resistance_15m": 108.0,
        "support_15m": 97.0,
        "range_60m": 30.0,
        "funding_rate": 0.0,
    }
    features.update(feature_overrides)
    now = clock()
    book = book_from_levels(
        [BookLevel(price=99.99, quantity=50), BookLevel(price=99.9, quantity=50)],
        [BookLevel(price=100.01, quantity=50), BookLevel(price=100.1, quantity=50)],
        now,
    )
    return MarketSnapshot(
        timestamp=now,
        symbol="BTCUSDT",
        market_type=MarketType.FUTURES,
        raw_market_data_reference="test",
        price=100.0,
        best_bid=99.99,
        best_ask=100.01,
        spread=0.02,
        spread_bps=2.0,
        features=features,
        data_quality=DataQuality(stale=False, book_available=True, candles_available=True, trades_available=True),
        quantitative_regime="bull_trend|normal_volatility",
        trigger="test",
    ), book


def attach_book(eng: TradingEngine, book) -> None:
    state = eng.states["BTCUSDT"]
    state.book = book
    state.best_bid = book.bids[0].price
    state.best_ask = book.asks[0].price
    state.last_price = 100.0
    state.last_book_at = clock()


def weights() -> BaselineWeightConfig:
    return BaselineWeightConfig()
