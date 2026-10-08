from __future__ import annotations


def shifted_feature_names(short_history: dict, long_history: dict, *, tolerance: float = 1e-12) -> list[str]:
    """Names whose value at the same close changes when older bars are prepended.

    An exponential average and a Wilder smooth do this. A return over a fixed
    tail does not, once that tail is already inside both series.
    """
    shifted: list[str] = []
    for name in sorted(set(short_history) & set(long_history)):
        left = short_history[name]
        right = long_history[name]
        if isinstance(left, bool) or isinstance(right, bool):
            continue
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            continue
        if abs(float(left) - float(right)) > tolerance:
            shifted.append(name)
    return shifted
