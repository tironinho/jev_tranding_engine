from __future__ import annotations

import math

from app.domain.mathutil import clip, ensure_utc
from datetime import datetime


def freshness(age_seconds: float | None, tau_seconds: float) -> float | None:
    """Exponential decay. A future timestamp is not usable."""
    if age_seconds is None or tau_seconds <= 0 or not math.isfinite(age_seconds) or not math.isfinite(tau_seconds):
        return None
    if age_seconds < 0:
        return None
    value = math.exp(-age_seconds / tau_seconds)
    if not math.isfinite(value):
        return None
    return clip(value, 0.0, 1.0)


def age_seconds(now: datetime, source_timestamp: datetime | None) -> float | None:
    if source_timestamp is None:
        return None
    return (ensure_utc(now) - ensure_utc(source_timestamp)).total_seconds()
