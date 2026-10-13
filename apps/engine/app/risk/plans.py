"""Ex-ante target plan used by JEV and the execution risk audit."""
from __future__ import annotations

import math

from app.domain.enums import Action
from app.risk.economics import compute_trade_economics, plan_geometry, target_for_net_rr


def candidate_plans(snapshot, side, limits, fee_rate, intelligence=None, *, allow_structure_extension=False):
    entry = (snapshot.best_ask if side is Action.LONG else snapshot.best_bid) or snapshot.price
    geometry = plan_geometry(side, entry, snapshot.features, limits)
    if isinstance(geometry, str):
        return []
    if limits.market_range_plans:
        return market_range_plans(snapshot, side, limits, fee_rate, intelligence, geometry)
    distance = abs(entry - geometry.stop)
    direction = 1 if side is Action.LONG else -1
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
    original_target = target
    if limits.cost_aware_target_floor:
        target = target_for_net_rr(side=side, entry=entry, stop=geometry.stop, target=target,
            min_net_rr=limits.min_net_rr, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
            exit_slippage_per_unit=entry*limits.plan_stress_bps/10000, funding_per_unit=-interest)
    reward = abs(target-entry)
    if not math.isfinite(target) or target <= 0 or (target-entry)*direction <= 0:
        return []
    econ = compute_trade_economics(side=side, entry=entry, stop=geometry.stop, target=target,
        quantity=1, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
        exit_slippage_per_unit=entry * limits.plan_stress_bps / 10000,
        funding_cashflow_total=-interest, fee_source="plan_estimate")
    label = f"rr_{econ.gross_rr:.2f}".rstrip("0").rstrip(".") if econ.gross_rr is not None else "target"
    return [{"plan_id": label, "entry": entry, "stop": geometry.stop, "target": target,
        "original_target": original_target,
        "target_extended_for_costs": abs(target-entry) > abs(original_target-entry)+1e-12,
        "target_policy": "net_rr_floor_v1" if limits.cost_aware_target_floor else limits.target_rule_version,
        "gross_rr": econ.gross_rr, "net_rr": econ.net_rr, "net_risk_per_unit": econ.net_risk,
        "net_reward_per_unit": econ.net_reward, "break_even_probability": 1 / (1 + econ.net_rr) if econ.net_rr and econ.net_rr > 0 else 1,
        "horizon_minutes": limits.max_hold_minutes, "round_trip_fee_rate": fee_rate * 2,
        "stress_bps": limits.plan_stress_bps, "borrow_hourly_rate": rate,
        "borrow_stress_rate": assumed_rate,
        "interest_estimate_per_unit": interest, "interest_known": rate is not None or snapshot.market_type.value != "margin",
        "requires_structure_break": reward > calculated_limit + max(1e-12, entry * 1e-10),
        "stop_policy": "breakeven_at_1R_lock_1R_at_2R" if limits.step_stop_to_breakeven else "fixed",
        "eligible": True, "reason": None}]


def market_range_plans(snapshot, side, limits, fee_rate, intelligence, geometry):
    """Cost-aware targets bounded by observed range, not a scaled-up hourly guess."""
    entry = (snapshot.best_ask if side is Action.LONG else snapshot.best_bid) or snapshot.price
    direction = 1 if side is Action.LONG else -1
    features = snapshot.features
    derivatives = (intelligence or {}).get("derivatives") or {}
    raw_rate = derivatives.get("quote_borrow_hourly_interest" if side is Action.LONG else "borrow_hourly_interest")
    rate = float(raw_rate) if isinstance(raw_rate, (int, float)) and math.isfinite(raw_rate) and raw_rate >= 0 else None
    stress_rate = max(rate or 0, limits.borrow_hourly_stress_rate)
    horizons = [60]
    aligned = all(isinstance(features.get(k), (int, float)) and math.isfinite(features[k])
                  and features[k] * direction > 0 for k in
                  ("return_5m", "return_15m", "return_60m", "return_240m",
                   "context_15m_slope", "taker_flow_1m", "taker_flow_5m"))
    if (limits.max_hold_minutes >= 240 and aligned
            and features.get("ema_alignment") == direction
            and isinstance(features.get("volume_ratio"), (int, float))
            and features["volume_ratio"] > 1):
        horizons.append(240)
    plans = []
    for horizon in horizons:
        span = features.get(f"range_{horizon}m")
        if not isinstance(span, (int, float)) or not math.isfinite(span) or span <= 0:
            continue
        interest = entry * stress_rate * math.ceil(horizon/60) if snapshot.market_type.value == "margin" else 0
        original = entry + direction * min(span, abs(entry-geometry.stop)*limits.rr_target_multiple)
        target = target_for_net_rr(side=side, entry=entry, stop=geometry.stop, target=original,
            min_net_rr=limits.min_net_rr, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
            exit_slippage_per_unit=entry*limits.plan_stress_bps/10000, funding_per_unit=-interest)
        reward = abs(target-entry)
        valid = target > 0 and (target-entry)*direction > 0 and reward <= span + entry*1e-10
        econ = compute_trade_economics(side=side, entry=entry, stop=geometry.stop, target=target,
            quantity=1, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
            exit_slippage_per_unit=entry*limits.plan_stress_bps/10000,
            funding_cashflow_total=-interest, fee_source="plan_estimate")
        plans.append({"plan_id": f"market_range_{horizon}m", "entry": entry, "stop": geometry.stop,
            "target": target, "original_target": original, "target_policy": "market_range_v1",
            "target_extended_for_costs": reward > abs(original-entry)+1e-12,
            "gross_rr": econ.gross_rr, "net_rr": econ.net_rr, "net_risk_per_unit": econ.net_risk,
            "net_reward_per_unit": econ.net_reward,
            "break_even_probability": 1/(1+econ.net_rr) if econ.net_rr and econ.net_rr > 0 else 1,
            "horizon_minutes": horizon, "max_hold_minutes": horizon,
            "reference_range_minutes": horizon, "reference_range_fraction": span/entry,
            "round_trip_fee_rate": fee_rate*2, "stress_bps": limits.plan_stress_bps,
            "borrow_hourly_rate": rate, "borrow_stress_rate": stress_rate,
            "interest_estimate_per_unit": interest, "interest_known": rate is not None or snapshot.market_type.value != "margin",
            "requires_structure_break": True, "stop_policy": "fixed", "eligible": valid,
            "reason": None if valid else "MARKET_RANGE_BELOW_COST_TARGET"})
    return plans


def score_probability(plan, probability, haircut):
    """Stress scenario, not a statistically calibrated confidence bound."""
    conservative = max(0, probability - haircut)
    loss, gain = plan["net_risk_per_unit"], plan["net_reward_per_unit"]
    expected = conservative * gain - (1 - conservative) * loss
    return {"raw_probability": probability, "stress_probability": conservative,
            "probability_status": "UNCALIBRATED", "expected_net_per_unit": expected,
            "expected_net_r": expected / loss if loss > 0 else -1,
            "probability_haircut": haircut}
