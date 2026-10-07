from __future__ import annotations

from app.evolution.schemas import REJECTION_GATES, REJECTION_LIVE, ExperimentStatus


def promotion_decision(
    *,
    target: str,
    gates: dict[str, bool],
    comparison: dict | None,
    policy: dict,
    auto: bool,
    auto_shadow: bool,
    auto_paper: bool,
) -> tuple[bool, str]:
    """Shadow and paper can be requested manually. Live cannot be granted by this engine."""
    if target in {"live", "canary", "champion_live"}:
        return False, REJECTION_LIVE
    required = ["static", "unit", "backtest", "oos"]
    if policy.get("require_walk_forward", True):
        required.append("walk_forward")
    if policy.get("require_monte_carlo", True):
        required.append("monte_carlo")
    if any(not gates.get(name) for name in required):
        return False, REJECTION_GATES
    if comparison:
        delta = comparison.get("expectancy_delta")
        drawdown_delta = comparison.get("drawdown_delta")
        challenger = comparison.get("challenger") or {}
        if challenger.get("trades", 0) < policy.get("min_trades", 100):
            return False, "INSUFFICIENT_SAMPLE"
        if delta is None or delta < policy.get("min_expectancy_improvement", 0.05):
            return False, "LOW_EFFECT_SIZE"
        if drawdown_delta is None or drawdown_delta > policy.get("max_drawdown_increase", 0.0):
            return False, "DRAWDOWN_INCREASE"
        if (challenger.get("profit_factor") or 0) < policy.get("min_profit_factor", 1.2):
            return False, "LOW_EFFECT_SIZE"
    else:
        return False, REJECTION_GATES
    if target == "shadow" and auto and not auto_shadow:
        return False, "AUTO_SHADOW_DISABLED"
    if target == "paper" and auto and not auto_paper:
        return False, "AUTO_PAPER_DISABLED"
    if target == "paper":
        shadow_ok = gates.get("shadow_sample", False)
        if not shadow_ok:
            return False, "SHADOW_GATE"
    return True, "accepted"


def next_status(target: str) -> ExperimentStatus:
    if target == "shadow":
        return ExperimentStatus.SHADOW
    if target == "paper":
        return ExperimentStatus.PAPER
    if target == "candidate":
        return ExperimentStatus.PROMOTED
    return ExperimentStatus.REJECTED
