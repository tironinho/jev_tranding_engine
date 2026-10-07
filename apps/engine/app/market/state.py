from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candle:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool
    timeframe: str


@dataclass
class AggTrade:
    timestamp: datetime
    price: float
    quantity: float
    aggressive_side: str  # "buy" or "sell"


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


@dataclass
class OrderBook:
    bids: list[Level]
    asks: list[Level]
    timestamp: datetime


@dataclass
class SymbolMarketState:
    symbol: str
    market_type: str
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    trades: list[AggTrade] = field(default_factory=list)
    book: OrderBook | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    last_price: float | None = None
    last_book_at: datetime | None = None
    last_trade_at: datetime | None = None
    last_event_at: datetime | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    previous_funding_rate: float | None = None
    open_interest: float | None = None
    previous_open_interest: float | None = None
    latency_ms: float | None = None
    dropped_out_of_order: int = 0
    max_trades: int = 20_000

    def add_trade(self, trade: AggTrade) -> None:
        if self.last_trade_at is not None and trade.timestamp < self.last_trade_at:
            self.dropped_out_of_order += 1
            return
        self.trades.append(trade)
        if len(self.trades) > self.max_trades:
            self.trades = self.trades[-self.max_trades :]
        self.last_trade_at = trade.timestamp
        self.last_price = trade.price
        self.last_event_at = trade.timestamp

    def upsert_candle(self, candle: Candle) -> None:
        series = self.candles.setdefault(candle.timeframe, [])
        if series and series[-1].open_time == candle.open_time:
            series[-1] = candle
        elif not series or candle.open_time > series[-1].open_time:
            series.append(candle)
        else:
            # Out-of-order historical repair: insert only if this open is new.
            if all(existing.open_time != candle.open_time for existing in series):
                series.append(candle)
                series.sort(key=lambda item: item.open_time)
        if len(series) > 1500:
            del series[:-1500]
        self.last_event_at = candle.close_time if candle.closed else candle.open_time
        if candle.closed:
            self.last_price = candle.close
