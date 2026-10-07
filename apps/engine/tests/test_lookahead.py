from datetime import timedelta

from app.features.engine import compute_features
from app.market.state import Candle, SymbolMarketState


def _candle(index: int, price: float) -> Candle:
    start = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc) + timedelta(minutes=index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=price,
        high=price + 0.2,
        low=price - 0.2,
        close=price + 0.05,
        volume=10,
        closed=True,
        timeframe="1m",
    )


def test_future_candles_do_not_change_features_at_t():
    state = SymbolMarketState(symbol="BTCUSDT", market_type="futures")
    candles = [_candle(i, 100 + i * 0.01) for i in range(40)]
    state.candles["1m"] = list(candles)
    as_of = candles[20].close_time
    price = candles[20].close
    before, _quality = compute_features(state, as_of, price)
    future = candles[30]
    candles[30] = Candle(
        open_time=future.open_time,
        close_time=future.close_time,
        open=1,
        high=500,
        low=0.1,
        close=400,
        volume=10_000,
        closed=True,
        timeframe="1m",
    )
    state.candles["1m"] = list(candles)
    after, _quality = compute_features(state, as_of, price)
    assert before == after


def test_closed_filter_ignores_a_bar_that_has_not_closed():
    state = SymbolMarketState(symbol="ETHUSDT", market_type="spot")
    known = _candle(0, 100)
    state.candles["1m"] = [known]
    as_of = known.close_time
    forming = Candle(
        open_time=known.close_time + timedelta(milliseconds=1),
        close_time=known.close_time + timedelta(minutes=1),
        open=100,
        high=130,
        low=90,
        close=125,
        volume=999,
        closed=False,
        timeframe="1m",
    )
    state.candles["1m"].append(forming)
    features, _quality = compute_features(state, as_of, known.close)
    assert features["return_1m"] is None or features["current_volume"] == known.volume
