from __future__ import annotations

from app.evolution.schemas import Anomaly


def anomaly_score(item: Anomaly) -> float:
    """Higher means the anomaly should be researched first. Win rate is not an input."""
    sample = max(item.sample_size, 1)
    impact = abs(item.expectancy or 0.0) * sample
    factor_damage = 0.0
    if item.profit_factor is not None and item.profit_factor < 1:
        factor_damage = 1 - item.profit_factor
    drawdown = float(item.evidence.get("max_drawdown") or 0.0)
    frequency = float(item.evidence.get("frequency") or sample)
    recency = float(item.evidence.get("recency") or 1.0)
    return item.anomaly_confidence * (impact + factor_damage * sample + drawdown * sample) * recency * (1 + frequency / 1000)


def rank_anomalies(anomalies: list[Anomaly]) -> list[Anomaly]:
    """One anomaly per family, code and symbol. Correlated duplicates stay behind the strongest."""
    ordered = sorted(anomalies, key=anomaly_score, reverse=True)
    kept: list[Anomaly] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in ordered:
        key = (item.strategy_family.value, item.code, item.symbol)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept
