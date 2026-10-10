"""Ex-ante target plan used by JEV and the execution risk audit."""
from __future__ import annotations

import math

from app.domain.enums import Action
from app.risk.economics import compute_trade_economics, plan_geometry


def candidate_plans(snapshot, side, limits, fee_rate, intelligence=None, *, allow_structure_extension=False):
    entry = (snapshot.best_ask if side is Action.LONG else snapshot.best_bid) or snapshot.price
    geometry = plan_geometry(side, entry, snapshot.features, limits)
    if isinstance(geometry, str):
        return []
    distance = abs(entry - geometry.stop)
    direction = 1 if side is Action.LONG else -1
    reward = abs(geometry.target - entry)
    calculated_limit = float(snapshot.features["range_60m"])
    boundary_key = "resistance_15m" if side is Action.LONG else "support_15m"
    boundary = snapshot.features.get(boundary_key)
    if isinstance(boundary, (int, float)) and (float(boundary) - entry) * direction > 0:
        calculated_limit = min(calculated_limit, abs(float(boundary) - entry))
    derivative = (intelligence or {}).get("derivatives") or {}
    rate = derivative.get("quote_borrow_hourly_interest" if side is Action.LONG else "borrow_hourly_interest")
    rate = float(rate) if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0 else None
    assumed_rate = max(rate or 0, limits.borrow_hourly_stress_rate)
    interest = entry * assumed_rate * max(1, math.ceil(limits.max_hold_minutes / 60)) if snapshot.market_type.value == "margin" else 0
    target = geometry.target
    if target <= 0:
        return []
    econ = compute_trade_economics(side=side, entry=entry, stop=geometry.stop, target=target,
        quantity=1, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
        exit_slippage_per_unit=entry * limits.plan_stress_bps / 10000,
        funding_cashflow_total=-interest, fee_source="plan_estimate")
    label = f"rr_{econ.gross_rr:.2f}".rstrip("0").rstrip(".") if econ.gross_rr is not None else "target"
    return [{"plan_id": label, "entry": entry, "stop": geometry.stop, "target": target,
        "gross_rr": econ.gross_rr, "net_rr": econ.net_rr, "net_risk_per_unit": econ.net_risk,
        "net_reward_per_unit": econ.net_reward, "break_even_probability": 1 / (1 + econ.net_rr) if econ.net_rr and econ.net_rr > 0 else 1,
        "horizon_minutes": limits.max_hold_minutes, "round_trip_fee_rate": fee_rate * 2,
        "stress_bps": limits.plan_stress_bps, "borrow_hourly_rate": rate,
        "borrow_stress_rate": assumed_rate,
        "interest_estimate_per_unit": interest, "interest_known": rate is not None or snapshot.market_type.value != "margin",
        "requires_structure_break": reward > calculated_limit + max(1e-12, entry * 1e-10),
        "stop_policy": "breakeven_at_1R_lock_1R_at_2R" if limits.step_stop_to_breakeven else "fixed",
        "eligible": True, "reason": None}]


def score_probability(plan, probability, haircut):
    """Stress scenario, not a statistically calibrated confidence bound."""
    conservative = max(0, probability - haircut)
    loss, gain = plan["net_risk_per_unit"], plan["net_reward_per_unit"]
    expected = conservative * gain - (1 - conservative) * loss
    return {"raw_probability": probability, "stress_probability": conservative,
            "probability_status": "UNCALIBRATED", "expected_net_per_unit": expected,
            "expected_net_r": expected / loss if loss > 0 else -1,
            "probability_haircut": haircut}
