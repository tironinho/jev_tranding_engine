from __future__ import annotations

from dataclasses import dataclass

from app.config import RiskLimits
from app.domain.enums import (
    HOUR_RANGE_UNAVAILABLE,
    STOP_ATR_FALLBACK,
    STOP_TOO_TIGHT,
    STOP_WIDENED_TO_MIN,
    STOP_TOO_WIDE,
    Action,
)
from app.domain.schemas import CostBreakdown, TradeEconomics

MIN_TARGET_R = 3.0


def rate_for(liquidity: str, maker: float, taker: float) -> float:
    return maker if liquidity == "maker" else taker


def funding_cashflow(side: Action, funding_rate: float | None, notional: float, periods: float) -> float:
    """Positive cashflow means the account receives funding."""
    if not funding_rate or periods <= 0 or notional <= 0:
        return 0.0
    if side is Action.LONG:
        return -funding_rate * notional * periods
    if side is Action.SHORT:
        return funding_rate * notional * periods
    return 0.0


def funding_periods(limits: RiskLimits) -> float:
    if not limits.apply_funding:
        return 0.0
    if limits.expected_hold_minutes < limits.funding_interval_minutes:
        return 0.0
    return limits.expected_hold_minutes / limits.funding_interval_minutes


@dataclass(frozen=True)
class Geometry:
    stop: float
    target: float
    reasons: tuple[str, ...]


def plan_geometry(
    side: Action,
    entry: float,
    features: dict,
    limits: RiskLimits,
) -> Geometry | str:
    atr = features.get("atr")
    if not isinstance(atr, (int, float)) or atr <= 0 or entry <= 0:
        return "MISSING_ATR"
    buffer = limits.atr_buffer_mult * float(atr)
    reasons: list[str] = []
    if side is Action.LONG:
        swing = features.get("recent_swing_low")
        structural = float(swing) - buffer if isinstance(swing, (int, float)) else None
        if structural is None or structural >= entry:
            structural = entry - limits.atr_stop_mult * float(atr)
            reasons.append(STOP_ATR_FALLBACK)
        if structural >= entry:
            return "STOP_INVALID"
        distance = entry - structural
        if distance / entry > limits.max_stop_pct:
            return STOP_TOO_WIDE
        floor = _stop_floor(entry, float(atr), features, limits)
        if distance < floor:
            structural = entry - floor
            distance = floor
            reasons.append(STOP_WIDENED_TO_MIN)
        target = _fit_target(side, entry, distance, features, limits)
        if isinstance(target, str):
            return target
        return Geometry(stop=structural, target=target, reasons=tuple(reasons))

    swing = features.get("recent_swing_high")
    structural = float(swing) + buffer if isinstance(swing, (int, float)) else None
    if structural is None or structural <= entry:
        structural = entry + limits.atr_stop_mult * float(atr)
        reasons.append(STOP_ATR_FALLBACK)
    if structural <= entry:
        return "STOP_INVALID"
    distance = structural - entry
    if distance / entry > limits.max_stop_pct:
        return STOP_TOO_WIDE
    floor = _stop_floor(entry, float(atr), features, limits)
    if distance < floor:
        structural = entry + floor
        distance = floor
        reasons.append(STOP_WIDENED_TO_MIN)
    target = _fit_target(side, entry, distance, features, limits)
    if isinstance(target, str):
        return target
    return Geometry(stop=structural, target=target, reasons=tuple(reasons))


def _stop_floor(entry: float, atr: float, features: dict, limits: RiskLimits) -> float:
    """Clear the fee minimum, the ATR multiple, and the widest of the last five 1m bars."""
    recent = features.get("range_1m")
    span = float(recent) if isinstance(recent, (int, float)) and recent > 0 else 0.0
    return max(limits.min_stop_pct * entry, limits.atr_stop_mult * atr, span)


def _fit_target(side: Action, entry: float, distance: float, features: dict, limits: RiskLimits) -> float | str:
    """Use 3R as the target floor while preserving a farther calculated target."""
    hour = features.get("range_60m")
    if not isinstance(hour, (int, float)) or hour <= 0 or distance <= 0:
        return HOUR_RANGE_UNAVAILABLE
    calculated = float(hour)
    boundary_key = "resistance_15m" if side is Action.LONG else "support_15m"
    boundary = features.get(boundary_key)
    if isinstance(boundary, (int, float)):
        boundary_reward = (float(boundary) - entry) if side is Action.LONG else (entry - float(boundary))
        if boundary_reward > 0:
            calculated = min(calculated, boundary_reward)
    reward = max(distance * max(MIN_TARGET_R, limits.rr_target_multiple), calculated)
    if reward <= 0:
        return HOUR_RANGE_UNAVAILABLE
    if side is Action.LONG:
        return entry + reward
    fitted = entry - reward
    if fitted <= 0:
        return HOUR_RANGE_UNAVAILABLE
    return fitted


def compute_trade_economics(
    *,
    side: Action,
    entry: float,
    stop: float,
    target: float,
    quantity: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
    exit_slippage_per_unit: float,
    funding_cashflow_total: float,
    fee_source: str,
    spread_cost: float = 0.0,
    slippage_cost: float = 0.0,
    spread_included_in_fill: bool = True,
) -> TradeEconomics:
    qty = abs(quantity)
    if side is Action.LONG:
        gross_risk = max(0.0, entry - stop) * qty
        gross_reward = max(0.0, target - entry) * qty
        exit_fee_stop = abs(stop) * qty * exit_fee_rate
        exit_fee_target = abs(target) * qty * exit_fee_rate
    else:
        gross_risk = max(0.0, stop - entry) * qty
        gross_reward = max(0.0, entry - target) * qty
        exit_fee_stop = abs(stop) * qty * exit_fee_rate
        exit_fee_target = abs(target) * qty * exit_fee_rate
    entry_fee = abs(entry) * qty * entry_fee_rate
    slip_total = abs(exit_slippage_per_unit) * qty
    # funding_cashflow_total is signed: positive means the account receives funding.
    net_risk = gross_risk + entry_fee + exit_fee_stop + slip_total + spread_cost + slippage_cost - funding_cashflow_total
    net_reward = gross_reward - entry_fee - exit_fee_target - slip_total - spread_cost - slippage_cost + funding_cashflow_total
    gross_rr = (gross_reward / gross_risk) if gross_risk > 0 else None
    net_rr = (net_reward / net_risk) if net_risk > 0 else None
    costs = CostBreakdown(
        entry_fee=entry_fee,
        exit_fee_at_target=exit_fee_target,
        exit_fee_at_stop=exit_fee_stop,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost + slip_total,
        funding_cashflow=funding_cashflow_total,
        fee_rate_entry=entry_fee_rate,
        fee_rate_exit=exit_fee_rate,
        fee_source=fee_source,
        spread_included_in_fill=spread_included_in_fill,
    )
    return TradeEconomics(
        entry=entry,
        stop=stop,
        target=target,
        quantity=qty,
        gross_risk=gross_risk,
        gross_reward=gross_reward,
        gross_rr=gross_rr,
        net_risk=net_risk,
        net_reward=net_reward,
        net_rr=net_rr,
        costs=costs,
    )


def size_quantity(
    *,
    equity: float,
    risk_fraction: float,
    entry: float,
    stop: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
    exit_slippage_per_unit: float,
) -> float:
    max_loss = equity * risk_fraction
    price_risk = abs(entry - stop)
    per_unit = price_risk + abs(entry) * entry_fee_rate + abs(stop) * exit_fee_rate + abs(exit_slippage_per_unit)
    if per_unit <= 0 or max_loss <= 0:
        return 0.0
    return max_loss / per_unit
