from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.domain.schemas import TradeRecord
from app.evolution.metrics_comparator import summarize
from app.evolution.schemas import Anomaly, StrategyFamily


def bootstrap_mean_interval(values: list[float], seed: int, draws: int = 400) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    size = len(values)
    for _ in range(draws):
        sample = [values[rng.randrange(size)] for _ in range(size)]
        means.append(sum(sample) / size)
    means.sort()
    low = means[int(0.025 * (draws - 1))]
    high = means[int(0.975 * (draws - 1))]
    return low, high


def detect_anomalies(
    trades: list[TradeRecord],
    *,
    min_sample: int,
    starting_equity: float,
    now: datetime | None = None,
) -> list[Anomaly]:
    """Only emits an anomaly when the sample clears the minimum and the loss is not noise."""
    moment = now or datetime.now(timezone.utc)
    found: list[Anomaly] = []
    grouped: dict[tuple[str, str], list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.strategy, trade.symbol)].append(trade)
    for (strategy, symbol), rows in grouped.items():
        if strategy not in {item.value for item in StrategyFamily}:
            continue
        if len(rows) < min_sample:
            continue
        summary = summarize(rows, starting_equity)
        expectancy = summary.get("expectancy_r")
        profit_factor = summary.get("profit_factor")
        if expectancy is None or expectancy >= 0 or (profit_factor is not None and profit_factor >= 1):
            continue
        interval = bootstrap_mean_interval([trade.r_multiple for trade in rows if trade.r_multiple is not None], seed=len(rows))
        if interval is None or interval[1] >= 0:
            continue
        found.append(
            Anomaly(
                code="NEGATIVE_EXPECTANCY",
                strategy_family=StrategyFamily(strategy),
                symbol=symbol,
                detected_at=moment,
                sample_size=len(rows),
                expectancy=expectancy,
                profit_factor=profit_factor,
                anomaly_confidence=0.95,
                evidence={"bootstrap_r_interval": interval, "window": f"last_{len(rows)}_trades"},
                window=f"last_{len(rows)}_trades",
            )
        )
    recent_cut = moment - timedelta(days=30)
    for strategy in {item.value for item in (StrategyFamily.BASELINE, StrategyFamily.BASELINE_JEV, StrategyFamily.BASELINE_OPENAI_JEV)}:
        recent = [trade for trade in trades if trade.strategy == strategy and trade.closed_at >= recent_cut]
        history = [trade for trade in trades if trade.strategy == strategy and trade.closed_at < recent_cut]
        if len(recent) < min_sample or len(history) < min_sample:
            continue
        recent_summary = summarize(recent, starting_equity)
        historical = summarize(history, starting_equity)
        if recent_summary.get("expectancy_r") is None or historical.get("expectancy_r") is None:
            continue
        if recent_summary["expectancy_r"] < historical["expectancy_r"] - 0.1:
            found.append(
                Anomaly(
                    code="STRATEGY_DECAY",
                    strategy_family=StrategyFamily(strategy),
                    symbol=None,
                    detected_at=moment,
                    sample_size=len(recent),
                    expectancy=recent_summary["expectancy_r"],
                    profit_factor=recent_summary.get("profit_factor"),
                    anomaly_confidence=0.8,
                    evidence={"historical_expectancy_r": historical["expectancy_r"], "recent_trades": len(recent)},
                    window="30d_vs_history",
                )
            )
    return found
