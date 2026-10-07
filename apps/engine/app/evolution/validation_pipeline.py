from __future__ import annotations

from app.evolution.paths import protected_changes
from app.evolution.schemas import REJECTION_PROTECTED, REJECTION_TESTS

STAGE_ORDER = (
    "static",
    "unit",
    "integration",
    "backtest",
    "oos",
    "walk_forward",
    "monte_carlo",
    "shadow",
    "paper",
    "candidate",
)


def run_static_checks(changed_paths: list[str], *, tests_passed: bool, lookahead_passed: bool) -> dict:
    blocked = protected_changes(changed_paths)
    reasons: list[str] = []
    if blocked:
        reasons.append(REJECTION_PROTECTED)
    if not tests_passed:
        reasons.append(REJECTION_TESTS)
    if not lookahead_passed:
        reasons.append("LOOKAHEAD_BIAS")
    return {"passed": not reasons, "reasons": reasons, "protected": blocked}


def historical_gates(static_ok: bool, tests_ok: bool, backtest_ok: bool, oos_ok: bool, walk_ok: bool, monte_ok: bool) -> dict[str, bool]:
    return {
        "static": static_ok,
        "unit": tests_ok,
        "backtest": backtest_ok,
        "oos": oos_ok,
        "walk_forward": walk_ok,
        "monte_carlo": monte_ok,
    }
