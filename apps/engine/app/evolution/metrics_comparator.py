from __future__ import annotations

from app.analytics.performance import summarize_trades
from app.domain.schemas import TradeRecord


def strategy_score(summary: dict, weights: dict, ai_cost: float = 0.0) -> float | None:
    """Configurable diagnostic score. Weights are not a claim of optimality."""
    if not summary.get("trades"):
        return None
    expectancy = summary.get("expectancy_r") or 0.0
    profit_factor = summary.get("profit_factor") or 0.0
    sharpe = summary.get("sharpe") or 0.0
    drawdown = summary.get("max_drawdown") or 0.0
    costs = (summary.get("fees_total") or 0.0) + (summary.get("slippage_total") or 0.0) + (summary.get("funding_total") or 0.0)
    equity = summary.get("starting_equity") or 1.0
    cost_ratio = costs / equity if equity else 0.0
    ai_ratio = ai_cost / equity if equity else 0.0
    return (
        weights.get("expectancy", 1.0) * expectancy
        + weights.get("profit_factor", 0.5) * profit_factor
        + weights.get("sharpe", 0.25) * sharpe
        - weights.get("drawdown_penalty", 1.0) * drawdown
        - weights.get("execution_cost_penalty", 0.5) * cost_ratio
        - weights.get("ai_cost_penalty", 0.5) * ai_ratio
    )


def compare_reports(champion: dict, challenger: dict, weights: dict) -> dict:
    left = strategy_score(champion, weights)
    right = strategy_score(challenger, weights)
    return {
        "champion": champion,
        "challenger": challenger,
        "champion_score": left,
        "challenger_score": right,
        "score_delta": None if left is None or right is None else right - left,
        "expectancy_delta": _delta(challenger.get("expectancy_r"), champion.get("expectancy_r")),
        "drawdown_delta": _delta(challenger.get("max_drawdown"), champion.get("max_drawdown")),
    }


def summarize(trades: list[TradeRecord], starting_equity: float) -> dict:
    report = summarize_trades(trades, starting_equity)
    report["starting_equity"] = starting_equity
    return report


def _delta(left, right):
    if left is None or right is None:
        return None
    return left - right
