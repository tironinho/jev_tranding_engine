"""Read-only OHLC audit. Does not infer fills or calibrate historical model outputs."""
from __future__ import annotations


def observed_barriers(bars, *, start_ms, end_ms, entry, stop, target, side):
    # Partial entry candles contain prices from BEFORE the decision. Partial end
    # candles contain future data. Neither is admissible for this comparison.
    rows = sorted((b for b in bars if b[0] >= start_ms and b[6] <= end_ms), key=lambda b: b[0])
    direction = 1 if side == "LONG" else -1
    outcome, hit = "NO_BARRIER_OBSERVED", None
    for bar in rows:
        high, low = float(bar[2]), float(bar[3])
        hit_stop = low <= stop if direction == 1 else high >= stop
        hit_target = high >= target if direction == 1 else low <= target
        if hit_stop or hit_target:
            outcome = "AMBIGUOUS" if hit_stop and hit_target else "STOP" if hit_stop else "TARGET"
            hit = bar[0]
            break
    complete = bool(rows) and rows[0][0] == start_ms and rows[-1][6] >= end_ms - 1
    complete = complete and all(b[0] == a[6] + 1 for a, b in zip(rows, rows[1:]))
    return {"observed_barrier": outcome, "hit_bar_open_ms": hit, "complete_horizon": complete,
            "bars": len(rows), "observation_start_ms": rows[0][0] if rows else None,
            "observation_end_ms": rows[-1][6] if rows else None,
            "directional_return": direction * (float(rows[-1][4]) / entry - 1) if rows else None,
            "policy": "fixed_original_barriers_not_execution"}
