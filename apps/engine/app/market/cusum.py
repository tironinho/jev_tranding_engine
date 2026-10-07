from __future__ import annotations


class CusumFilter:
    """Fire only after the price has accumulated about one ATR of drift.

    A 1-minute close by itself is not an event. The first print only sets
    the reference price.
    """

    def __init__(self, atr_multiple: float = 1.0) -> None:
        self.atr_multiple = atr_multiple
        self._price: dict[str, float] = {}
        self._positive: dict[str, float] = {}
        self._negative: dict[str, float] = {}

    def event(self, symbol: str, price: float, atr: float | None) -> bool:
        previous = self._price.get(symbol)
        self._price[symbol] = price
        if previous is None or previous <= 0 or price <= 0:
            return False
        if atr is None or atr <= 0:
            return False
        change = price / previous - 1.0
        threshold = self.atr_multiple * (atr / price)
        positive = max(0.0, self._positive.get(symbol, 0.0) + change)
        negative = min(0.0, self._negative.get(symbol, 0.0) + change)
        if positive >= threshold or negative <= -threshold:
            self._positive[symbol] = 0.0
            self._negative[symbol] = 0.0
            return True
        self._positive[symbol] = positive
        self._negative[symbol] = negative
        return False
