from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.enums import MarketType
from app.execution.slippage import book_from_levels
from app.domain.schemas import BookLevel
from app.features.engine import build_snapshot
from app.market.state import Candle, SymbolMarketState
from app.service import TradingEngine


def _book(price: float, timestamp: datetime):
    bids = [BookLevel(price=price * (1 - 0.00005 * (index + 1)), quantity=5) for index in range(20)]
    asks = [BookLevel(price=price * (1 + 0.00005 * (index + 1)), quantity=5) for index in range(20)]
    return book_from_levels(bids, asks, timestamp)


async def replay_candles(engine: TradingEngine, symbol: str, candles: list[Candle]) -> TradingEngine:
    """Push historical closed candles through the same snapshot, strategy, risk and paper path."""
    state = engine.states.setdefault(symbol, SymbolMarketState(symbol=symbol, market_type=engine.settings.market_type))
    for candle in candles:
        if not candle.closed:
            raise ValueError("replay only accepts closed candles")
        state.upsert_candle(candle)
        state.last_price = candle.close
        state.best_bid = candle.close * (1 - 0.0001)
        state.best_ask = candle.close * (1 + 0.0001)
        state.last_book_at = candle.close_time
        state.book = _book(candle.close, candle.close_time)
        snapshot = build_snapshot(
            state,
            as_of=candle.close_time,
            trigger="replay",
            stale_after_ms=engine.settings.stale_after_ms,
        )
        if snapshot is None:
            continue
        if snapshot.market_type is not MarketType(engine.settings.market_type):
            raise RuntimeError("market type drifted")
        await engine.evaluate_snapshot(snapshot)
        await engine.manage_positions(symbol, as_of=candle.close_time)
    return engine


def export_dataset_csv(engine: TradingEngine, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for decision in engine.store.decisions:
        snapshot = engine.store.snapshots.get(decision["snapshot_id"], {})
        features = snapshot.get("features") or {}
        label = engine.store.labels.get(decision["snapshot_id"], {})
        trade = next((item for item in engine.store.trades if item["decision_id"] == decision["decision_id"]), {})
        row = {
            "snapshot_id": decision["snapshot_id"],
            "decision_id": decision["decision_id"],
            "opportunity_id": decision["opportunity_id"],
            "strategy": decision["strategy"],
            "action": decision["action"],
            "confidence": decision["confidence"],
            "net_pnl": trade.get("net_pnl"),
            "mfe": trade.get("mfe"),
            "mae": trade.get("mae"),
        }
        for key, value in features.items():
            if isinstance(value, (int, float, str, bool)) or value is None:
                row[f"f_{key}"] = value
        for key, value in label.items():
            row[key] = value
        rows.append(row)
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    _try_parquet(rows, path.with_suffix(".parquet"))
    return len(rows)


def _try_parquet(rows: list[dict], path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception:
        return
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path)


def synthetic_trend(start: datetime, count: int, price: float = 100.0, step: float = 0.05) -> list[Candle]:
    candles = []
    cursor = price
    for index in range(count):
        open_time = start + timedelta(minutes=index)
        close_time = open_time + timedelta(minutes=1) - timedelta(milliseconds=1)
        nxt = cursor + step
        candles.append(
            Candle(
                open_time=open_time,
                close_time=close_time,
                open=cursor,
                high=max(cursor, nxt) + 0.01,
                low=min(cursor, nxt) - 0.01,
                close=nxt,
                volume=10 + index % 3,
                closed=True,
                timeframe="1m",
            )
        )
        cursor = nxt
    return candles


def utc(minute: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minute)
