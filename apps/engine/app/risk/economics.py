from __future__ import annotations

from dataclasses import dataclass

from app.config import RiskLimits
from app.domain.enums import (
    NO_STRUCTURE_TARGET,
    STOP_ATR_FALLBACK,
    STOP_TOO_TIGHT,
    STOP_WIDENED_TO_MIN,
    STOP_TOO_WIDE,
    Action,
)
from app.domain.schemas import CostBreakdown, TradeEconomics


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
        if distance / entry < limits.min_stop_pct:
            structural = entry * (1 - limits.min_stop_pct)
            distance = entry - structural
            reasons.append(STOP_WIDENED_TO_MIN)
        target = _target_long(entry, distance, features, limits)
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
    if distance / entry < limits.min_stop_pct:
        structural = entry * (1 + limits.min_stop_pct)
        distance = structural - entry
        reasons.append(STOP_WIDENED_TO_MIN)
    target = _target_short(entry, distance, features, limits)
    if isinstance(target, str):
        return target
    return Geometry(stop=structural, target=target, reasons=tuple(reasons))


def _target_long(entry: float, distance: float, features: dict, limits: RiskLimits) -> float | str:
    if limits.target_mode.value == "fixed":
        return entry * (1 + limits.fixed_target_pct)
    if limits.target_mode.value == "rr":
        return entry + distance * limits.rr_target_multiple
    # Estrutura de 15 minutos só limita o lucro quando paga pelo menos o múltiplo do stop.
    # Um nível colado na entrada não serve: o alvo vai para esse múltiplo.
    rr_target = entry + distance * limits.rr_target_multiple
    resistance = features.get("resistance_15m")
    if isinstance(resistance, (int, float)) and float(resistance) > entry:
        if limits.target_fallback == "rr" and float(resistance) < rr_target:
            return rr_target
        return float(resistance)
    if limits.target_fallback == "rr":
        return rr_target
    return NO_STRUCTURE_TARGET


def _target_short(entry: float, distance: float, features: dict, limits: RiskLimits) -> float | str:
    if limits.target_mode.value == "fixed":
        return entry * (1 - limits.fixed_target_pct)
    if limits.target_mode.value == "rr":
        return entry - distance * limits.rr_target_multiple
    rr_target = entry - distance * limits.rr_target_multiple
    support = features.get("support_15m")
    if isinstance(support, (int, float)) and float(support) < entry:
        if limits.target_fallback == "rr" and float(support) > rr_target:
            return rr_target
        return float(support)
    if limits.target_fallback == "rr":
        return rr_target
    return NO_STRUCTURE_TARGET


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


def target_for_min_net_rr(
    *,
    side: Action,
    entry: float,
    stop: float,
    quantity: float,
    entry_fee_rate: float,
    exit_fee_rate: float,
    exit_slippage_per_unit: float,
    funding_cashflow_total: float,
    spread_cost: float,
    slippage_cost: float,
    min_net_rr: float,
) -> float | None:
    """Price at which net reward / net risk equals min_net_rr. None when costs make that impossible."""
    qty = abs(quantity)
    if qty <= 0 or entry <= 0 or exit_fee_rate >= 1:
        return None
    slip = abs(exit_slippage_per_unit) * qty
    entry_fee = abs(entry) * qty * entry_fee_rate
    extra = slip + spread_cost + slippage_cost
    if side is Action.LONG:
        gross_risk = max(0.0, entry - stop) * qty
        exit_fee_stop = abs(stop) * qty * exit_fee_rate
        net_risk = gross_risk + entry_fee + exit_fee_stop + extra - funding_cashflow_total
        if net_risk <= 0:
            return None
        numerator = min_net_rr * net_risk + entry * qty + entry_fee + extra - funding_cashflow_total
        denom = qty * (1 - exit_fee_rate)
        if denom <= 0:
            return None
        return numerator / denom
    gross_risk = max(0.0, stop - entry) * qty
    exit_fee_stop = abs(stop) * qty * exit_fee_rate
    net_risk = gross_risk + entry_fee + exit_fee_stop + extra - funding_cashflow_total
    if net_risk <= 0:
        return None
    numerator = entry * qty - entry_fee - extra + funding_cashflow_total - min_net_rr * net_risk
    denom = qty * (1 + exit_fee_rate)
    if denom <= 0 or numerator <= 0:
        return None
    return numerator / denom


def extend_target_for_costs(
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
    spread_cost: float,
    slippage_cost: float,
    min_net_rr: float,
) -> tuple[float, bool]:
    """Move the target only far enough to clear costs, and only by at most one round trip.

    A minimum net RR of 50 is a real rejection. A 2.5R target that lands at 1.4996 is the fee on the winner.
    """
    required = target_for_min_net_rr(
        side=side,
        entry=entry,
        stop=stop,
        quantity=quantity,
        entry_fee_rate=entry_fee_rate,
        exit_fee_rate=exit_fee_rate,
        exit_slippage_per_unit=exit_slippage_per_unit,
        funding_cashflow_total=funding_cashflow_total,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        min_net_rr=min_net_rr * (1 + 1e-5),
    )
    if required is None:
        return target, False
    slack = abs(entry) * (entry_fee_rate + exit_fee_rate) + abs(exit_slippage_per_unit)
    cushion = slack + abs(entry) * 1e-6
    if side is Action.LONG:
        if required <= target or required <= entry:
            return target, False
        if required - target <= cushion:
            return required, True
        return target, False
    if required >= target or required >= entry or required <= 0:
        return target, False
    if target - required <= cushion:
        return required, True
    return target, False
