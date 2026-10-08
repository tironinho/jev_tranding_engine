from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from app.domain.mathutil import ensure_utc

T = TypeVar("T")


def asof_backward(rows: list[T], decision_time: datetime, observed_at) -> T | None:
    """Latest row at or before the decision. A later row is never selected."""
    cutoff = ensure_utc(decision_time)
    chosen: T | None = None
    chosen_at: datetime | None = None
    for row in rows:
        stamp = ensure_utc(observed_at(row))
        if stamp > cutoff:
            continue
        if chosen_at is None or stamp > chosen_at:
            chosen = row
            chosen_at = stamp
    return chosen
