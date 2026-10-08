from __future__ import annotations

import math


def relative_disagreement(left: float | None, right: float | None, epsilon: float = 1e-12) -> float | None:
    if left is None or right is None:
        return None
    if not math.isfinite(left) or not math.isfinite(right):
        return None
    return abs(left - right) / max(abs(left), abs(right), epsilon)


def feature_quality(
    *,
    available: bool,
    fresh: float | None,
    disagreement: float | None = None,
    sample_sufficient: bool = True,
    provider_up: bool = True,
) -> float:
    if not available or not provider_up:
        return 0.0
    score = 1.0 if fresh is None else max(0.0, min(1.0, fresh))
    if not sample_sufficient:
        score *= 0.5
    if disagreement is not None and math.isfinite(disagreement):
        score *= max(0.0, 1.0 - min(disagreement, 1.0))
    return score


def missing_feature() -> dict:
    """Unknown is not zero."""
    return {"value": None, "available": False, "valid_for_decision": False}
