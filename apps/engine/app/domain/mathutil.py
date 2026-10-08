from __future__ import annotations

import math
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(num: float, den: float) -> float | None:
    if den == 0 or not math.isfinite(den) or not math.isfinite(num):
        return None
    return num / den


def round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.floor((value / step) + 1e-9)
    return round(steps * step, 12)


def round_up_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    steps = math.ceil((value / step) - 1e-9)
    return round(steps * step, 12)


def bps(fraction: float) -> float:
    return fraction * 10_000


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
