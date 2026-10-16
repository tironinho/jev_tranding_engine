"""Prospective entry selection; context, structure, flow and timing have separate jobs."""
import math
from datetime import datetime
from app.domain.enums import Action

VERSION = "context_entry_v2"
REPEAT_VERSION = "post_entry_retest_v1"
MAX_EXTENSION_ATR = 1.0


def repeat_setup(side, features, last_entry_at, now):
    """An impulse/elapsed time alone is not a new setup. Missing provenance fails closed."""
    suffix = "long" if side == Action.LONG else "short"
    events = []
    if side in (Action.LONG, Action.SHORT):
        for name, clock in ((f"retest_{suffix}_5m", "structure_bar_open_5m"),
                            (f"reclaim_retest_{suffix}_5m", "structure_bar_open_5m"),
                            (f"pullback_resume_{suffix}_5m", "pullback_bar_open_5m")):
            if features.get(name) is not True or last_entry_at is None:
                continue
            try:
                start = datetime.fromisoformat(features[clock].replace("Z", "+00:00"))
                if last_entry_at < start <= now:
                    events.append(name)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    return {"version": REPEAT_VERSION, "fresh_setup": bool(events), "events": events}

def classify_entry(side, features):
    direction = 1 if side == Action.LONG else -1
    keys = ("context_15m_slope", "price_vs_ema20_15m", "distance_to_vwap",
            "return_1m", "return_5m", "return_15m", "taker_flow_1m", "taker_flow_5m")
    missing = [k for k in keys if not isinstance(features.get(k), (float, int))
               or isinstance(features.get(k), bool) or not math.isfinite(features[k])]
    if side not in (Action.LONG, Action.SHORT):
        return {"version": VERSION, "setup": "NONE", "eligible": False, "reasons": ["NO_CANDIDATE"]}
    if missing:
        return {"version": VERSION, "setup": "UNKNOWN", "eligible": False,
                "reasons": ["CONTEXT_DATA_UNAVAILABLE"], "missing_features": missing}
    slope = direction * features["context_15m_slope"] > 0
    ema = direction * features["price_vs_ema20_15m"] > 0
    aligned = slope and ema
    flow = all(direction * features[k] > 0 for k in ("taker_flow_1m", "taker_flow_5m"))
    returns = all(direction * features[k] > 0 for k in ("return_5m", "return_15m"))
    vwap = direction * features["distance_to_vwap"] > 0
    suffix = "long" if direction == 1 else "short"
    retest = features.get(f"reclaim_retest_{suffix}_5m") is True
    # A confirmed reclaim retest includes the earlier reclaim and its held level.
    reclaim = retest or features.get("bullish_reclaim_5m" if direction == 1 else "bearish_reclaim_5m") is True
    sustained = retest or features.get(f"reclaim_sustained_{suffix}_5m") is True
    structure = any(features.get(key) is True for key in (
        "breakout_5m" if direction == 1 else "breakdown_5m",
        f"retest_{suffix}_5m", f"pullback_resume_{suffix}_5m"))
    extension_key = "extension_above_resistance_5m_atr" if direction == 1 else "extension_below_support_5m_atr"
    extension = features.get(extension_key)
    timing_known = (isinstance(extension, (float, int)) and not isinstance(extension, bool)
                    and math.isfinite(extension))
    extended = extension > MAX_EXTENSION_ATR if timing_known else None
    fading = direction * features["return_1m"] < 0 < direction * features["return_5m"]
    setup = "CONTINUATION" if aligned else "REVERSAL"
    checks = {"context_aligned": aligned, "context_slope_aligned": slope, "context_ema_aligned": ema,
              "persistent_flow": flow, "returns_aligned": returns, "vwap_aligned": vwap,
              "structure_confirmed": structure, "reclaim": reclaim, "reclaim_sustained": sustained,
              "reclaim_retest": retest, "timing_known": timing_known,
              "not_extended": not extended if timing_known else None, "not_fading": not fading}
    reasons = []
    if not flow: reasons.append("ENTRY_FLOW_DIVERGENT")
    if not returns: reasons.append("ENTRY_RETURNS_DIVERGENT")
    if not vwap: reasons.append("ENTRY_VWAP_AGAINST")
    if aligned:
        if not structure: reasons.append("ENTRY_STRUCTURE_REQUIRED")
    else:
        if not reclaim: reasons.append("REVERSAL_RECLAIM_REQUIRED")
        if not sustained: reasons.append("REVERSAL_NOT_SUSTAINED")
        if not retest: reasons.append("REVERSAL_RETEST_REQUIRED")
    if not timing_known: reasons.append("ENTRY_TIMING_DATA_UNAVAILABLE")
    elif extended: reasons.append("ENTRY_EXTENDED_WAIT_RETEST")
    if fading: reasons.append("ENTRY_FADING_WAIT_RESUMPTION")
    return {"version": VERSION, "setup": setup, "eligible": not reasons,
            "checks": checks, "reasons": reasons,
            "timing": {"extension_atr": extension if timing_known else None,
                       "max_extension_atr": MAX_EXTENSION_ATR, "fading": fading,
                       "preferred_entry": "RETEST" if extended or not aligned else "RESUMPTION" if fading else "CONFIRMED_STRUCTURE"}}
