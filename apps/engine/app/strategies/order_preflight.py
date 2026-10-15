"""Necessary notional checks, not execution authorization or capital reservation."""
import math
from app.domain.enums import Action
from app.domain.mathutil import round_down_to_step
from app.risk.executable import minimum_executable_quantity, minimum_trade_room


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def order_preflight(plan, side, limits, rules, equity, fee_rate):
    unknown={"version":"order_preflight_v1", "status":"UNKNOWN", "can_authorize":False}
    if not plan or not rules or not _number(equity) or equity<=0:
        return {**unknown,"reason":"FRESH_CAPITAL_OR_RULES_UNAVAILABLE"}
    step=rules.get("step_size"); exchange_floor=rules.get("min_notional")
    if not _number(step) or step<=0 or not _number(exchange_floor) or exchange_floor<=0:
        return {**unknown,"reason":"EXCHANGE_RULES_UNAVAILABLE"}
    entry=plan.get("entry"); stop=plan.get("stop");target=plan.get("target")
    if not all(_number(v) and v>0 for v in [entry,stop,target]):
        return {**unknown,"reason":"PLAN_UNAVAILABLE"}
    valid = stop < entry < target if side is Action.LONG else target < entry < stop
    if side not in {Action.LONG, Action.SHORT} or not valid or not _number(fee_rate) or not 0 <= fee_rate < 1:
        return {**unknown,"reason":"PLAN_OR_FEES_UNAVAILABLE"}
    hard=[equity*limits.max_symbol_exposure*limits.max_leverage,
          equity*limits.max_total_exposure*limits.max_leverage]
    if limits.max_portfolio_notional_fraction>0:hard.append(equity*limits.max_portfolio_notional_fraction)
    minimum=max(exchange_floor,limits.min_order_notional)
    required=minimum_executable_quantity(entry,stop,target,side,minimum,fee_rate,step)
    if required is None:
        return {**unknown,"reason":"PLAN_UNAVAILABLE"}
    trade=minimum_trade_room(equity, limits.max_trade_notional_fraction,
                             limits.max_minimum_trade_notional_fraction, 0, required*entry)
    # The per-trade fraction may rise to the entry and exit minimum. Stop risk
    # and the other ceilings stay hard, so this does not enlarge a normal order.
    budget=min(min(hard), trade)
    ceiling=budget*1.01
    qty=round_down_to_step(ceiling/entry,step)
    exit_qty=round_down_to_step(qty*(1-fee_rate if side is Action.LONG else 1),step)
    exit_notional=exit_qty*min(entry,target,stop*.998)
    stop_cash=abs(entry-stop)*required + required*(entry+abs(stop))*fee_rate
    if stop_cash > equity*limits.max_risk_per_trade + 1e-6:
        reason="PREFLIGHT_MINIMUM_EXCEEDS_STOP_RISK"
    else:
        reason=("PREFLIGHT_ENTRY_BELOW_MIN_NOTIONAL" if qty*entry+1e-9<minimum else
                "PREFLIGHT_EXIT_BELOW_MIN_NOTIONAL" if exit_notional+1e-9<minimum*1.01 else None)
    return dict(version="order_preflight_v1",status="INFEASIBLE" if reason else "NOTIONAL_POSSIBLE",
                reason=reason,can_authorize=False,optimistic_notional_ceiling=ceiling,
                estimated_entry_notional=qty*entry,estimated_exit_notional=exit_notional,
                minimum_entry_notional=minimum,minimum_exit_notional=minimum*1.01,
                remaining_capital_checked=False)
