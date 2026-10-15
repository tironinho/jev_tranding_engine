"""Shadow reading with one job per evidence group.

Context classifies the setup. Structure justifies an entry. Flow confirms it.
Volume only changes how strong that confirmation is. RSI and ROC describe
timing and never cast a directional vote. Spread, depth and stale data are
filters: a strong RSI cannot offset them.

This record does not authorize an order and does not change the 0.45 baseline
floor. Partial disagreement stays visible as MIXED instead of becoming an
automatic veto.
"""
from __future__ import annotations

import math

from app.domain.enums import Action

VERSION = "role_baseline_v2"
# The spread gate already published by the active baseline. Not a new cutoff.
DEFAULT_SPREAD_LIMIT_BPS = 8.0
# One ATR of travel past the 5m level. A unit conversion, not a fitted edge.
_EXTENSION_ATR = 1.0

_CONTEXT = ("context_15m_slope", "price_vs_ema20_15m", "distance_to_vwap")


def assess_roles(side, features, *, stale: bool = False, spread_limit_bps: float = DEFAULT_SPREAD_LIMIT_BPS) -> dict:
    direction = 1 if side in (Action.LONG, "LONG") else -1 if side in (Action.SHORT, "SHORT") else 0
    if not direction:
        return {"version": VERSION, "mode": "SHADOW", "can_authorize": False,
                "setup": "NONE", "eligible": False, "rank": None, "reasons": ["NO_CANDIDATE"]}
    context, context_votes = _context(features, direction)
    event, structure_known = _event(features, direction)
    flow = _flow(features, direction)
    extension = _extension(features, direction)
    extended = extension is not None and extension > _EXTENSION_ATR
    fading = _fading(features, direction)
    filters = _filters(features, stale=stale, spread_limit_bps=spread_limit_bps)
    setup = ("CONTINUATION" if context == "ALIGNED" else "REVERSAL" if context in {"CONTRARY", "MIXED"} else "UNKNOWN")
    reasons: list[str] = []
    if context == "UNKNOWN":
        reasons.append("CONTEXT_DATA_UNAVAILABLE")
    if not structure_known:
        reasons.append("STRUCTURE_DATA_UNAVAILABLE")
    elif event == "NONE":
        reasons.append("STRUCTURE_EVENT_REQUIRED")
    if not flow["persistent"]:
        reasons.append("IMPULSE_WITHOUT_SUSTAINED_FLOW" if flow["impulse_only"] else "FLOW_NOT_CONFIRMED")
    if setup == "REVERSAL" and event != "RECLAIM":
        reasons.append("REVERSAL_RECLAIM_REQUIRED")
    structure_ok = event in {"BREAK", "RETEST", "RECLAIM"}
    continuation = context == "ALIGNED" and structure_ok and flow["persistent"]
    reversal = context in {"CONTRARY", "MIXED"} and event == "RECLAIM" and flow["persistent"]
    eligible = not filters and (continuation or reversal)
    return {
        "version": VERSION, "mode": "SHADOW", "can_authorize": False,
        "setup": setup, "context": context, "context_votes": context_votes,
        "structure_event": event, "eligible": eligible,
        "entry_variants": {
            "immediate": {"eligible": eligible},
            "confirmed_retest": {"eligible": not filters and context == "ALIGNED" and event == "RETEST" and flow["persistent"] and not fading},
            "sustained_reversal": {"eligible": not filters and reversal and features.get("reclaim_sustained_long_5m" if direction > 0 else "reclaim_sustained_short_5m") is True},
        },
        "rank": _rank(event, flow, features, extension, fading),
        "extended": extended, "fading": fading,
        "participation": _participation(features),
        "timing": {"rsi": _finite(features.get("rsi")), "roc": _finite(features.get("roc")),
                   "extension_atr": extension, "extended": extended, "fading": fading,
                   "one_minute_break": features.get("breakout") is True or features.get("breakdown") is True},
        "flow": flow, "filters": filters, "reasons": reasons,
        "thresholds": {"spread_limit_bps": spread_limit_bps, "extension_atr": _EXTENSION_ATR},
    }


def _context(features: dict, direction: int) -> tuple[str, dict]:
    votes = {key: _agree(features.get(key), direction) for key in _CONTEXT}
    known = [value for value in votes.values() if value is not None]
    if len(known) < 2:
        return "UNKNOWN", votes
    score = sum(known)
    if score > 0:
        return "ALIGNED", votes
    if score < 0:
        return "CONTRARY", votes
    return "MIXED", votes


def _event(features: dict, direction: int) -> tuple[str, bool]:
    if direction > 0:
        names = ("bullish_reclaim_5m", "retest_long_5m", "breakout_5m")
        labels = ("RECLAIM", "RETEST", "BREAK")
    else:
        names = ("bearish_reclaim_5m", "retest_short_5m", "breakdown_5m")
        labels = ("RECLAIM", "RETEST", "BREAK")
    flags = [_flag(features.get(name)) for name in names]
    if any(flag is None for flag in flags) and not any(flag is True for flag in flags):
        return "UNKNOWN", False
    for flag, label in zip(flags, labels, strict=True):
        if flag is True:
            return label, True
    return "NONE", True


def _flow(features: dict, direction: int) -> dict:
    one = _agree(features.get("taker_flow_1m"), direction)
    five = _agree(features.get("taker_flow_5m"), direction)
    return {"taker_1m": one, "taker_5m": five,
            "persistent": one == 1 and five == 1,
            "impulse_only": one == 1 and five != 1}


def _filters(features: dict, *, stale: bool, spread_limit_bps: float) -> list[str]:
    reasons = []
    if stale:
        reasons.append("STALE_MARKET_DATA")
    spread = _finite(features.get("spread_bps"))
    if spread is None or spread >= spread_limit_bps:
        reasons.append("SPREAD_FILTER")
    atr_value = _finite(features.get("atr"))
    if atr_value is None or atr_value <= 0:
        reasons.append("ATR_UNAVAILABLE")
    bid = _finite(features.get("bid_liquidity_10"))
    ask = _finite(features.get("ask_liquidity_10"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        reasons.append("DEPTH_UNAVAILABLE")
    return reasons


def _rank(event: str, flow: dict, features: dict, extension: float | None, fading: bool) -> float:
    structure = 1.0 if event in {"BREAK", "RETEST", "RECLAIM"} else 0.0
    if flow["persistent"]:
        confirmation = 1.0
    elif flow["taker_5m"] == 1 and flow["taker_1m"] != -1:
        confirmation = 0.25
    else:
        confirmation = 0.0
    participation = _participation(features)
    quality = 0.5 if participation is None else participation
    timing = 1.0 if extension is None else 1.0 / (1.0 + max(extension, 0.0))
    if fading:
        timing *= 0.5
    return structure * confirmation * quality * timing


def _participation(features: dict) -> float | None:
    volume = _finite(features.get("volume_ratio"))
    if volume is None or volume < 0:
        return None
    return min(volume, 2.0) / 2.0


def _extension(features: dict, direction: int) -> float | None:
    key = "extension_above_resistance_5m_atr" if direction > 0 else "extension_below_support_5m_atr"
    return _finite(features.get(key))


def _fading(features: dict, direction: int) -> bool:
    recent = _finite(features.get("return_1m"))
    setup = _finite(features.get("return_5m"))
    return recent is not None and setup is not None and recent * direction < 0 < setup * direction


def _agree(value, direction: int) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    if number * direction > 0:
        return 1
    if number * direction < 0:
        return -1
    return 0


def _flag(value) -> bool | None:
    if value is True or value is False:
        return value
    return None


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)
