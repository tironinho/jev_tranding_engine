from datetime import datetime, timezone

from app.market.parse import apply_market_message, parse_rest_klines
from app.market.state import SymbolMarketState


def test_agg_trade_buyer_maker_is_aggressive_sell():
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
                "v": "3",
                "x": True,
            },
        }
    }
    kind = apply_market_message(state, payload, datetime.fromtimestamp(1_700_000_060, tz=timezone.utc))
    assert kind == "kline_close_1m"
    assert state.candles["1m"][-1].closed is True


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
