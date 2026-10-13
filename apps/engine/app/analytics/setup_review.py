"""Exploratory cohort, never an entry permission or calibrated probability."""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

VERSION = "aligned_break_flow_v1"


def classify_setup(decision: dict, snapshot: dict | None) -> dict:
    meta = decision.get("metadata") or {}
    features = (snapshot or {}).get("features") or {}
    side = meta.get("baseline_action")
    sign = 1 if side == "LONG" else -1 if side == "SHORT" else 0

    def aligned(key):
        value = features.get(key)
        return bool(sign and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value * sign > 0)

    volume = features.get("volume_ratio")
    checks = {
        "baseline_candidate": bool(sign),
        "structural_break": bool(sign and features.get("breakout" if sign > 0 else "breakdown") is True),
        "ema_alignment": features.get("ema_alignment") == sign if sign else False,
        "returns_5m_15m_60m": all(aligned(k) for k in ("return_5m", "return_15m", "return_60m")),
        "context_15m": aligned("context_15m_slope"),
        "above_average_volume": isinstance(volume, (int, float)) and math.isfinite(volume) and volume > 1,
        "persistent_executed_flow": all(aligned(k) for k in ("taker_flow_1m", "taker_flow_5m")),
    }
    return {"version": VERSION, "mode": "SHADOW_RESEARCH", "matches": all(checks.values()),
            "candidate_side": side, "checks": checks,
            "missing_features": [k for k in ("return_5m", "return_15m", "return_60m",
                "context_15m_slope", "volume_ratio", "taker_flow_1m", "taker_flow_5m", "ema_alignment")
                if features.get(k) is None], "can_override_veto": False}


def review_setups(rows: list[dict]) -> dict:
    groups = {"matching": [], "other": []}
    evidence = []
    for row in rows:
        decision = row["decision"]
        setup = classify_setup(decision, row.get("snapshot"))
        groups["matching" if setup["matches"] else "other"].append(row)
        if setup["matches"]:
            evidence.append({"decision_id": decision["decision_id"], "timestamp": decision["timestamp"],
                "symbol": decision["symbol"], "action": decision["action"], "setup": setup,
                "jev_probability": decision.get("metadata", {}).get("jev_continuation"),
                "reason_codes": decision.get("reason_codes", []), "labels": row.get("labels"),
                "trades": row.get("trades", [])})
    summaries = {}
    for name, cohort in groups.items():
        trades = [t for row in cohort for t in row.get("trades", [])]
        profits = [t["net_pnl"] for t in trades]
        beyond = 0
        for row in cohort:
            horizon = (row["decision"].get("metadata", {}).get("jev_trade_plan") or {}).get("horizon_minutes")
            for t in row.get("trades", []):
                if horizon is not None:
                    minutes = (datetime.fromisoformat(t["closed_at"].replace("Z", "+00:00"))
                               - datetime.fromisoformat(t["opened_at"].replace("Z", "+00:00"))).total_seconds()/60
                    beyond += minutes > horizon
        summaries[name] = {"decisions": len(cohort), "actions": dict(Counter(r["decision"]["action"] for r in cohort)),
            "closed_trade_records": len(trades), "net_pnl": sum(profits) if profits else None,
            "win_rate": sum(p > 0 for p in profits)/len(profits) if profits else None,
            "closures_beyond_forecast_horizon": beyond}
    return {"version": VERSION, "mode": "SHADOW_RESEARCH", "sample_decisions": len(rows),
            "cohorts": summaries, "rows": evidence,
            "limitations": ["Exploratory conditions, not optimized or proof of an edge.",
                "Future return labels do not establish target-before-stop execution.",
                "Unexecuted and externally closed trades have no invented PnL.",
                "Partial closures are individual trade records; forecast horizon differs from holding time."]}
