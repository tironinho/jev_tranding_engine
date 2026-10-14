"""Point-in-time returns must not silently bridge gaps in market observations."""
from __future__ import annotations

import math

HORIZONS = {
    "future_return_30s": 30,
    "future_return_1m": 60,
    "future_return_3m": 180,
    "future_return_5m": 300,
    "future_return_15m": 900,
}
VERSION = "future_returns_v2"
# The durable fallback has one-minute snapshots. Preserve the actual elapsed
# time and permit at most one observation interval of lateness, never an outage.
MAX_LATENESS_SECONDS = 60


def observed_return(origin_at, origin_price, observed_at, observed_price, horizon_seconds):
    elapsed = (observed_at-origin_at).total_seconds()
    late = elapsed-horizon_seconds
    valid = (0 <= late <= MAX_LATENESS_SECONDS and math.isfinite(origin_price)
             and math.isfinite(observed_price) and origin_price > 0 and observed_price > 0)
    timing = {"observed_at": observed_at.isoformat(), "elapsed_seconds": elapsed,
              "lateness_seconds": late, "status": "VALID" if valid else "INVALID_OBSERVATION"}
    if late > MAX_LATENESS_SECONDS:
        timing['status'] = "OBSERVATION_GAP"
    return (observed_price/origin_price-1 if valid else None), timing


def has_valid_return(labels):
    return isinstance(labels, dict) and any(
        isinstance(labels.get(name), (int, float)) and not isinstance(labels[name], bool)
        and math.isfinite(labels[name]) for name in HORIZONS)
