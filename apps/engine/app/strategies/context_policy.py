"""Prospective context selection; no future labels or fitted thresholds."""
import math
from app.domain.enums import Action

VERSION = "context_entry_v1"

def classify_entry(side, features):
    direction = 1 if side is Action.LONG else -1
    keys = ("context_15m_slope", "distance_to_vwap", "return_5m", "return_15m", "taker_flow_1m", "taker_flow_5m")
    missing = [k for k in keys if not isinstance(features.get(k), (float, int))
               or isinstance(features.get(k), bool) or not math.isfinite(features[k])]
    if side not in (Action.LONG, Action.SHORT):
        return {"version": VERSION, "setup": "NONE", "eligible": False, "reasons": ["NO_CANDIDATE"]}
    if missing:
        return {"version": VERSION, "setup": "UNKNOWN", "eligible": False,
                "reasons": ["CONTEXT_DATA_UNAVAILABLE"], "missing_features": missing}
    aligned = direction * features["context_15m_slope"] > 0
    flow = all(direction * features[k] > 0 for k in ("taker_flow_1m", "taker_flow_5m"))
    returns = all(direction * features[k] > 0 for k in ("return_5m", "return_15m"))
    vwap = direction * features["distance_to_vwap"] > 0
    reclaim = features.get("bullish_reclaim" if direction == 1 else "bearish_reclaim") is True
    # Counter-context trades require a reclaim, not just a one-minute impulse.
    setup = "CONTINUATION" if aligned else "REVERSAL"
    eligible = flow and returns and vwap and (aligned or reclaim)
    checks = {"context_aligned": aligned, "persistent_flow": flow,
              "returns_aligned": returns, "vwap_aligned": vwap, "reclaim": reclaim}
    reasons = []
    if not flow: reasons.append("ENTRY_FLOW_DIVERGENT")
    if not returns: reasons.append("ENTRY_RETURNS_DIVERGENT")
    if not vwap: reasons.append("ENTRY_VWAP_AGAINST")
    if not aligned and not reclaim: reasons.append("REVERSAL_RECLAIM_REQUIRED")
    return {"version": VERSION, "setup": setup, "eligible": eligible,
            "checks": checks, "reasons": reasons}
