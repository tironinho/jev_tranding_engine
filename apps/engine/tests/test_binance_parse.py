from datetime import datetime, timedelta, timezone

from app.features.engine import compute_features
from app.market.parse import apply_market_message, parse_rest_klines
from app.market.state import Candle, SymbolMarketState


def test_spot_book_ticker_without_event_name_refreshes_the_book():
    state = SymbolMarketState("BTCUSDT", "spot")
    payload = {
        "stream": "btcusdt@bookTicker",
        "data": {"u": 1, "s": "BTCUSDT", "b": "100", "B": "1", "a": "100.1", "A": "2"},
    }
    when = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert apply_market_message(state, payload, when) == "book"
    assert state.best_bid == 100
    assert state.last_book_at == when


def test_spot_partial_depth_without_symbol_uses_bids_and_asks():
    state = SymbolMarketState("ETHUSDT", "spot")
    payload = {
        "stream": "ethusdt@depth20@100ms",
        "data": {"lastUpdateId": 1, "bids": [["200", "1"]], "asks": [["201", "1"]]},
    }
    when = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert apply_market_message(state, payload, when) == "depth"
    assert state.best_ask == 201
    assert state.last_book_at == when

    state = SymbolMarketState("BTCUSDT", "futures")
    payload = {
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "E": 1_700_000_000_000, "s": "BTCUSDT", "p": "100", "q": "2", "T": 1_700_000_000_000, "m": True},
    }
    kind = apply_market_message(state, payload, datetime.fromtimestamp(1_700_000_000, tz=timezone.utc))
    assert kind == "trade"
    assert state.trades[-1].aggressive_side == "sell"


def test_kline_close_is_the_snapshot_trigger():
    state = SymbolMarketState("ETHUSDT", "futures")
    payload = {
        "data": {
            "e": "kline",
            "E": 1_700_000_060_000,
            "s": "ETHUSDT",
            "k": {
                "t": 1_700_000_000_000,
                "T": 1_700_000_059_999,
                "i": "1m",
                "o": "10",
                "h": "11",
                "l": "9",
                "c": "10.5",
                "v": "10",
                "V": "3",
                "x": True,
            },
        }
    }
    kind = apply_market_message(state, payload, datetime.fromtimestamp(1_700_000_060, tz=timezone.utc))
    assert kind == "kline_close_1m"
    assert state.candles["1m"][-1].closed is True
    assert state.candles["1m"][-1].taker_buy_volume == 3


def test_crossed_book_is_dropped():
    state = SymbolMarketState("SOLUSDT", "spot")
    payload = {"data": {"e": "bookTicker", "E": 1_700_000_000_000, "s": "SOLUSDT", "b": "20", "a": "19", "B": "1", "A": "1"}}
    assert apply_market_message(state, payload, datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)) is None
    assert state.best_bid is None


def test_rest_klines_skip_garbage():
    candles = parse_rest_klines(
        "BTCUSDT",
        "1m",
        [
            [1_700_000_000_000, "1", "2", "0.5", "1.5", "10", 1_700_000_059_999],
            [1_700_000_060_000, "bad", "2", "1", "1", "1", 1_700_000_119_999],
        ],
    )
    assert len(candles) == 1
    assert candles[0].close == 1.5
    assert candles[0].taker_buy_volume is None


def test_rest_kline_keeps_taker_buy_volume():
    candles = parse_rest_klines(
        "BTCUSDT",
        "1m",
        [[1_700_000_000_000, "100", "101", "99", "100", "10", 1_700_000_059_999, "1000", 20, "3"]],
    )
    assert candles[0].taker_buy_volume == 3


def test_taker_flow_is_negative_when_selling_dominates():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(61):
        open_time = start + timedelta(minutes=index)
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1) - timedelta(milliseconds=1),
                open=100,
                high=100.2,
                low=99.8,
                close=100,
                volume=10,
                closed=True,
                timeframe="1m",
                taker_buy_volume=3,
            )
        )
    state = SymbolMarketState("BTCUSDT", "spot")
    state.candles["1m"] = candles
    features, _quality = compute_features(state, candles[-1].close_time, 110)
    assert features["taker_flow_1m"] == -0.4
    assert abs(features["range_1m"] - 0.4) < 1e-9
    assert abs(features["return_60m"] - 0.1) < 1e-9
