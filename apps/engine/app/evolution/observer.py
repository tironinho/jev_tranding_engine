from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.domain.schemas import TradeRecord
from app.evolution.metrics_comparator import summarize


WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}


class EvolutionObserver:
    def __init__(self, starting_equity: float) -> None:
        self.starting_equity = starting_equity

    def collect(self, trades: list[TradeRecord], now: datetime | None = None) -> dict:
        moment = now or datetime.now(timezone.utc)
        by_family: dict[str, list[TradeRecord]] = defaultdict(list)
        for trade in trades:
            by_family[trade.strategy].append(trade)
        families = {}
        for family, rows in by_family.items():
            ordered = sorted(rows, key=lambda item: item.closed_at)
            families[family] = {
                "all": summarize(ordered, self.starting_equity),
                "windows": {
                    name: summarize([trade for trade in ordered if trade.closed_at >= moment - delta], self.starting_equity)
                    for name, delta in WINDOWS.items()
                },
                "last_n": {str(size): summarize(ordered[-size:], self.starting_equity) for size in (50, 100, 500)},
            }
        return {"as_of": moment.isoformat(), "trade_count": len(trades), "families": families}
