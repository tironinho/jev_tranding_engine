from __future__ import annotations

from app.analytics.performance import summarize_trades
from app.domain.schemas import TradeRecord


def _index(trades: list[TradeRecord]) -> dict[str, TradeRecord]:
    return {str(trade.decision_id): trade for trade in trades}


def compare_strategies(
    *,
    baseline: list[TradeRecord],
    baseline_jev: list[TradeRecord],
    openai_jev: list[TradeRecord],
    decisions: list[dict],
    openai_cost: float | None,
    starting_equity: float,
) -> dict:
    """Relative outcomes. Incremental AI dollars stay null until a real cost is supplied."""
    by_opp: dict[str, dict[str, dict]] = {}
    for decision in decisions:
        opp = str(decision.get("opportunity_id"))
        by_opp.setdefault(opp, {})[decision.get("strategy")] = decision

    def acted(decision: dict | None) -> bool:
        return bool(decision and decision.get("action") in {"LONG", "SHORT"} and decision.get("signal_status") == "valid")

    only_baseline = 0
    only_jev = 0
    only_openai = 0
    all_agreed = 0
    disagreed = 0
    jev_eliminated = 0
    eliminated_ids: list[str] = []
    for group in by_opp.values():
        b = acted(group.get("baseline"))
        j = acted(group.get("baseline_jev"))
        o = acted(group.get("baseline_openai_jev"))
        flags = (b, j, o)
        if flags == (True, False, False):
            only_baseline += 1
        elif flags == (False, True, False):
            only_jev += 1
        elif flags == (False, False, True):
            only_openai += 1
        if b and j and o:
            actions = {group[key]["action"] for key in ("baseline", "baseline_jev", "baseline_openai_jev")}
            if len(actions) == 1:
                all_agreed += 1
            else:
                disagreed += 1
        elif sum(flags) >= 2 and len({group[k]["action"] for k, flag in (("baseline", b), ("baseline_jev", j), ("baseline_openai_jev", o)) if flag}) > 1:
            disagreed += 1
        if b and not j:
            jev_eliminated += 1
            eliminated_ids.append(str(group["baseline"]["decision_id"]))

    base_by_id = {str(trade.decision_id): trade for trade in baseline}
    eliminated_trades = [base_by_id[item] for item in eliminated_ids if item in base_by_id]
    base_summary = summarize_trades(baseline, starting_equity)
    jev_summary = summarize_trades(baseline_jev, starting_equity)
    openai_summary = summarize_trades(openai_jev, starting_equity)
    incremental_jev = jev_summary["net_pnl"] - base_summary["net_pnl"]
    incremental_openai = openai_summary["net_pnl"] - jev_summary["net_pnl"]
    ai_roi = None
    if openai_cost is not None and openai_cost > 0:
        ai_roi = incremental_openai / openai_cost
    return {
        "baseline_trades": base_summary["trades"],
        "jev_trades": jev_summary["trades"],
        "openai_trades": openai_summary["trades"],
        "jev_eliminated_signals": jev_eliminated,
        "eliminated_trade_net_pnl": sum(trade.net_pnl for trade in eliminated_trades),
        "approved_jev_net_pnl": jev_summary["net_pnl"],
        "only_baseline": only_baseline,
        "only_jev": only_jev,
        "only_openai": only_openai,
        "all_agreed": all_agreed,
        "disagreed": disagreed,
        "incremental_pnl_jev_vs_baseline": incremental_jev,
        "incremental_pnl_openai_vs_jev": incremental_openai,
        "incremental_expectancy_openai_vs_jev": _delta(openai_summary.get("expectancy"), jev_summary.get("expectancy")),
        "ai_cost": openai_cost,
        "ai_roi": ai_roi,
        "baseline": base_summary,
        "baseline_jev": jev_summary,
        "baseline_openai_jev": openai_summary,
    }


def _delta(left, right):
    if left is None or right is None:
        return None
    return left - right
