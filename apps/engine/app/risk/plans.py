"""Bounded, ex-ante trade alternatives. Never extend a target to force acceptance."""
from __future__ import annotations

import math

from app.domain.enums import Action
from app.risk.economics import compute_trade_economics, plan_geometry


def candidate_plans(snapshot, side, limits, fee_rate, intelligence=None):
    entry = (snapshot.best_ask if side is Action.LONG else snapshot.best_bid) or snapshot.price
    geometry = plan_geometry(side, entry, snapshot.features, limits)
    if isinstance(geometry, str):
        return []
    distance = abs(entry - geometry.stop)
    hour = snapshot.features.get("range_60m")
    if not isinstance(hour, (int, float)) or not math.isfinite(hour) or hour <= 0:
        return []
    direction = 1 if side is Action.LONG else -1
    boundaries = [float(hour)]
    # recent_high/recent_low often is the level that triggered the candidate. Treating
    # that already-touched level as future target capacity collapses every breakout
    # plan below the net R:R floor. Only the wider 15m structure caps the target.
    for key in ("resistance_15m",) if side is Action.LONG else ("support_15m",):
        level = snapshot.features.get(key)
        if isinstance(level, (int, float)) and (level - entry) * direction > 0:
            boundaries.append((level - entry) * direction)
    maximum = min(boundaries)
    derivative = (intelligence or {}).get("derivatives") or {}
    rate = derivative.get("quote_borrow_hourly_interest" if side is Action.LONG else "borrow_hourly_interest")
    rate = float(rate) if isinstance(rate, (int, float)) and math.isfinite(rate) and rate >= 0 else None
    assumed_rate = max(rate or 0, limits.borrow_hourly_stress_rate)
    interest = entry * assumed_rate * max(1, math.ceil(limits.max_hold_minutes / 60)) if snapshot.market_type.value == "margin" else 0
    rows, seen = [], set()
    for rr in (1.5, 2.0, 2.5, 3.0):
        reward = min(distance * rr, maximum)
        target = entry + direction * reward
        if target <= 0 or round(target, 10) in seen:
            continue
        seen.add(round(target, 10))
        econ = compute_trade_economics(side=side, entry=entry, stop=geometry.stop, target=target,
            quantity=1, entry_fee_rate=fee_rate, exit_fee_rate=fee_rate,
            exit_slippage_per_unit=entry * limits.plan_stress_bps / 10000,
            funding_cashflow_total=-interest, fee_source="plan_estimate")
        rows.append({"plan_id": f"rr_{rr:g}", "entry": entry, "stop": geometry.stop, "target": target,
            "gross_rr": econ.gross_rr, "net_rr": econ.net_rr, "net_risk_per_unit": econ.net_risk,
            "net_reward_per_unit": econ.net_reward, "break_even_probability": 1 / (1 + econ.net_rr) if econ.net_rr and econ.net_rr > 0 else 1,
            "horizon_minutes": limits.max_hold_minutes, "round_trip_fee_rate": fee_rate * 2,
            "stress_bps": limits.plan_stress_bps, "borrow_hourly_rate": rate,
            "borrow_stress_rate": assumed_rate,
            "interest_estimate_per_unit": interest, "interest_known": rate is not None or snapshot.market_type.value != "margin",
            "stop_policy": "breakeven_at_1R_lock_1R_at_2R" if limits.step_stop_to_breakeven else "fixed",
            "eligible": econ.net_rr is not None and econ.net_rr >= limits.min_net_rr,
            "reason": None if econ.net_rr is not None and econ.net_rr >= limits.min_net_rr else "NET_RR_TOO_LOW"})
    return rows


def score_probability(plan, probability, haircut):
    """Stress scenario, not a statistically calibrated confidence bound."""
    conservative = max(0, probability - haircut)
    loss, gain = plan["net_risk_per_unit"], plan["net_reward_per_unit"]
    expected = conservative * gain - (1 - conservative) * loss
    return {"raw_probability": probability, "stress_probability": conservative,
            "probability_status": "UNCALIBRATED", "expected_net_per_unit": expected,
            "expected_net_r": expected / loss if loss > 0 else -1,
            "probability_haircut": haircut}
