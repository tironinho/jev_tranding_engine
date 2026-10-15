"""Smallest quantity that clears the exchange entry floor and the projected exit."""
from __future__ import annotations

import math

from app.domain.enums import Action
from app.domain.mathutil import round_down_to_step, round_up_to_step


def minimum_trade_room(equity, fraction, minimum_fraction, exposure, required):
    """Keep the ordinary symbol budget; extend only a first entry to its minimum."""
    if fraction <= 0:
        return math.inf
    normal = equity * fraction - exposure
    if exposure > 1e-9 or minimum_fraction <= 0:
        return normal
    return max(normal, min(required, equity * minimum_fraction))


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def minimum_executable_quantity(entry, stop, target, side, floor, entry_fee_rate, step):
    """Step-aligned quantity whose entry and fee-rounded exit both clear the floor.

    The exit uses the same haircut and lowest price as the live risk check.
    Returns None when the levels cannot define that quantity.
    """
    if side not in {Action.LONG, Action.SHORT}:
        return None
    if not all(_positive(value) for value in (entry, stop, target, floor)):
        return None
    if not isinstance(entry_fee_rate, (int, float)) or isinstance(entry_fee_rate, bool):
        return None
    if not math.isfinite(entry_fee_rate) or not 0 <= entry_fee_rate < 1:
        return None
    factor = (1 - entry_fee_rate) if side is Action.LONG else 1.0
    lowest = min(entry, target, stop * 0.998)
    if factor <= 0 or lowest <= 0:
        return None
    raw = max(floor / entry, (floor * 1.01) / (lowest * factor))
    qty = round_up_to_step(raw, step) if _positive(step) else raw
    for _ in range(8):
        exit_qty = qty * factor
        if _positive(step):
            exit_qty = round_down_to_step(exit_qty, step)
        if entry * qty + 1e-9 >= floor and exit_qty * lowest + 1e-9 >= floor * 1.01:
            return qty
        if not _positive(step):
            return None
        qty = round(qty + step, 12)
    return None
