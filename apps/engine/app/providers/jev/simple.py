"""Seven exchange observations and a read-only description of the candidate."""
from __future__ import annotations

import math

from app.domain.enums import Action
from app.providers.jev.schemas import JevProviderError
from app.risk.economics import plan_geometry


_CONTEXT = (
    " Use only evidence. Missing values are unknown. _pct fields are percentages. "
    "Taker flow is signed: positive buys, negative sells; 1m overlaps 5m."
)
INSTRUCTIONS = {
    "trend_continuation_probability": (
        "Probability candidate_side reaches the supplied target before its stop or an exit "
        "under trade.time_exit, from the quoted entry. The review time is not a deadline "
        "when its rule keeps profitable positions open."
    ) + _CONTEXT,
    "reversal_probability": (
        "Probability price reverses against candidate_side and reaches the supplied stop "
        "before its target or time exit."
    ) + _CONTEXT,
    "false_breakout_probability": (
        "Probability the stated BREAKOUT/BREAKDOWN returns through its level before target "
        "or time exit. For NONE/UNKNOWN return 0.5; this answer is then ignored."
    ) + _CONTEXT,
}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _positive(value):
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _bounded(value, low, high):
    number = _finite(value)
    return number if number is not None and low <= number <= high else None


def _present(**values):
    # A real zero survives; an absent or invalid observation does not become zero.
    return {key: value for key, value in values.items() if value is not None}


def evidence_from_features(features):
    """Same observations the baseline already computed, without calling JEV."""
    features = features or {}
    alignment = _bounded(features.get("ema_alignment"), -1, 1)
    atr_fraction = _positive(features.get("atr_normalized"))
    price = _positive(features.get("close")) or _positive(features.get("price"))
    span = _positive(features.get("range_60m_frac"))
    if span is None and price and _positive(features.get("range_60m")):
        span = features["range_60m"] / price
    pct = lambda value: _finite(value * 100) if value is not None else None
    up, down = features.get("breakout"), features.get("breakdown")
    event = ("UNKNOWN" if up is True and down is True else "BREAKOUT" if up is True
             else "BREAKDOWN" if down is True else "NONE" if up is False and down is False else "UNKNOWN")
    volume = _finite(features.get("volume_ratio"))
    spread = _finite(features.get("spread_bps"))
    return {
        "trend": _present(ema_alignment=alignment, ema20_slope_5m_pct=pct(_finite(features.get("ema_20_slope"))),
                          price_vs_ema20_15m_pct=pct(_finite(features.get("price_vs_ema20_15m"))),
                          context_15m_slope_pct=pct(_finite(features.get("context_15m_slope"))),
                          distance_to_vwap_pct=pct(_finite(features.get("distance_to_vwap")))),
        "momentum": _present(rsi_14=_bounded(features.get("rsi"), 0, 100), roc_10m_pct=pct(_finite(features.get("roc"))),
                             return_5m_pct=pct(_finite(features.get("return_5m"))),
                             return_15m_pct=pct(_finite(features.get("return_15m"))),
                             return_60m_pct=pct(_finite(features.get("return_60m")))),
        "volume": _present(relative_to_previous_20m=volume if volume is not None and volume >= 0 else None),
        "flow": _present(taker_1m=_bounded(features.get("taker_flow_1m"), -1, 1),
                         taker_5m=_bounded(features.get("taker_flow_5m"), -1, 1)),
        "structure": _present(event=event),
        "volatility": _present(atr_14_pct=pct(atr_fraction), range_60m_pct=pct(span)),
        "liquidity": _present(spread_bps=spread if spread is not None and spread >= 0 else None),
    }


def observation_plan(snapshot, side, limits, round_trip_fee, execution_plan=None):
    """Describe existing geometry; never fit a target, size or authorize an order."""
    if limits is None:
        return {"error": "MISSING_RISK_GEOMETRY"}
    entry = _positive(snapshot.best_ask if side is Action.LONG else snapshot.best_bid) or _positive(snapshot.price)
    if entry is None or _positive(snapshot.features.get("atr")) is None:
        return {"error": "MISSING_PRICE_OR_ATR"}
    if execution_plan:
        entry, stop, target = (execution_plan.get(key) for key in ("entry", "stop", "target"))
    else:
        geometry = plan_geometry(side, entry, snapshot.features, limits)
        if isinstance(geometry, str):
            return {"error": geometry}
        stop, target = geometry.stop, geometry.target
    explicit_deadline = execution_plan and execution_plan.get("target_policy") == "market_range_v1"
    if explicit_deadline:
        rule, minutes = "EXIT_AT_DEADLINE", execution_plan.get("max_hold_minutes")
    elif not limits.time_exit_enabled or limits.max_hold_minutes <= 0:
        rule, minutes = "TARGET_OR_STOP", None
    elif limits.target_rule_version in {"initial_live_2026_10_08", "live_first_ten_2026_10_09"}:
        rule, minutes = "EXIT_IF_NET_PNL_NON_POSITIVE", limits.max_hold_minutes
    else:
        rule, minutes = "EXIT_UNLESS_NET_POSITIVE_WITH_BREAKEVEN_STOP", limits.max_hold_minutes
    return {"entry": entry, "stop": stop, "target": target, "observed_price": snapshot.price,
            "time_exit_rule": rule, "review_after_minutes": minutes,
            "round_trip_fee_rate": round_trip_fee, "estimate_only": True}


def _trade(plan, side):
    if not isinstance(plan, dict) or plan.get("error"):
        reason = plan.get("error", "missing") if isinstance(plan, dict) else "missing"
        raise JevProviderError("simple_input_geometry:" + str(reason))
    entry, stop, target = (_positive(plan.get(key)) for key in ("entry", "stop", "target"))
    if not all((entry, stop, target)):
        raise JevProviderError("simple_input_geometry:invalid_levels")
    if not (stop < entry < target if side == "LONG" else target < entry < stop):
        raise JevProviderError("simple_input_geometry:invalid_side")
    rule = plan.get("time_exit_rule")
    rules = {"EXIT_AT_DEADLINE", "TARGET_OR_STOP", "EXIT_IF_NET_PNL_NON_POSITIVE",
             "EXIT_UNLESS_NET_POSITIVE_WITH_BREAKEVEN_STOP"}
    if rule not in rules:
        raise JevProviderError("simple_input_geometry:invalid_time_rule")
    time_exit = {"rule": rule}
    if rule != "TARGET_OR_STOP":
        minutes = _positive(plan.get("review_after_minutes"))
        if minutes is None:
            raise JevProviderError("simple_input_geometry:missing_review_time")
        time_exit["after_minutes"] = minutes
    if rule in {"EXIT_IF_NET_PNL_NON_POSITIVE", "EXIT_UNLESS_NET_POSITIVE_WITH_BREAKEVEN_STOP"}:
        fee = _bounded(plan.get("round_trip_fee_rate"), 0, .5)
        if fee is None:
            raise JevProviderError("simple_input_geometry:missing_fee_estimate")
        rate = fee/2
        # Same fee-only net breakeven used by the current time-exit check,
        # estimated before the fill. No account balance or approval floor.
        distance = fee/(1-rate if side == "LONG" else 1+rate)
        time_exit["estimated_breakeven_distance_pct"] = distance*100
    target_pct, stop_pct = (_positive(abs(level/entry-1)*100) for level in (target, stop))
    if target_pct is None or stop_pct is None:
        raise JevProviderError("simple_input_geometry:invalid_distances")
    return {"target_distance_pct": target_pct, "stop_distance_pct": stop_pct,
            "levels_are_estimates": True, "time_exit": time_exit}


def state_for_model(payload):
    features = payload.get("features") or {}
    side = payload.get("baseline_action")
    if side not in {"LONG", "SHORT"}:
        raise JevProviderError("simple_input:missing_candidate")
    alignment = _bounded(features.get("ema_alignment"), -1, 1)
    rsi = _bounded(features.get("rsi"), 0, 100)
    atr_fraction = _positive(features.get("atr_normalized"))
    if alignment is None or rsi is None or atr_fraction is None:
        raise JevProviderError("simple_input:missing_trend_momentum_or_volatility")
    up, down = features.get("breakout"), features.get("breakdown")
    event = ("UNKNOWN" if up is True and down is True else "BREAKOUT" if up is True
             else "BREAKDOWN" if down is True else "NONE" if up is False and down is False else "UNKNOWN")
    plan = payload.get("observation_plan")
    trade = _trade(plan, side)
    # The observed midpoint and level are used only to derive a unitless
    # extension. Absolute prices and duplicated class labels stay off the wire.
    price = _positive(plan.get("observed_price"))
    atr = _positive(features.get("atr"))
    level = _positive(features.get("resistance" if event == "BREAKOUT" else "support"))
    extension = ((price-level)/atr if event == "BREAKOUT" else (level-price)/atr
                 if event == "BREAKDOWN" else None) if price and atr and level else None
    span = _positive(features.get("range_60m_frac"))
    if span is None and price and _positive(features.get("range_60m")):
        span = features["range_60m"]/price
    pct = lambda value: _finite(value*100) if value is not None else None
    volume = _finite(features.get("volume_ratio"))
    spread = _finite(features.get("spread_bps"))
    state = {"symbol": payload.get("symbol"), "market_type": payload.get("market_type"),
        "candidate_side": side, "candle_minutes": 1,
        "evidence": {
            "trend": _present(ema_alignment=alignment, ema20_slope_5m_pct=pct(_finite(features.get("ema_20_slope"))),
                              price_vs_ema20_15m_pct=pct(_finite(features.get("price_vs_ema20_15m"))),
                              context_15m_slope_pct=pct(_finite(features.get("context_15m_slope"))),
                              distance_to_vwap_pct=pct(_finite(features.get("distance_to_vwap")))),
            "momentum": _present(rsi_14=rsi, roc_10m_pct=pct(_finite(features.get("roc"))),
                                 return_5m_pct=pct(_finite(features.get("return_5m"))),
                                 return_15m_pct=pct(_finite(features.get("return_15m"))),
                                 return_60m_pct=pct(_finite(features.get("return_60m")))),
            "volume": _present(relative_to_previous_20m=volume if volume is not None and volume >= 0 else None),
            "flow": _present(taker_1m=_bounded(features.get("taker_flow_1m"), -1, 1),
                             taker_5m=_bounded(features.get("taker_flow_5m"), -1, 1)),
            "structure": _present(event=event, extension_from_break_level_atr=_finite(extension),
                                  reclaim_in_candidate_direction=(features.get("bullish_reclaim" if side == "LONG" else "bearish_reclaim")
                                      if isinstance(features.get("bullish_reclaim" if side == "LONG" else "bearish_reclaim"), bool) else None)),
            "volatility": _present(atr_14_pct=pct(atr_fraction), range_60m_pct=pct(span)),
            "liquidity": _present(spread_bps=spread if spread is not None and spread >= 0 else None),
        }, "trade": trade}
    from app.providers.jev.schemas import SETUP_MARKET_PROMPT
    if payload.get('prompt_version') == SETUP_MARKET_PROMPT:
        from app.strategies.context_policy import setup_guard
        guard = setup_guard(Action(side), features, payload.get('timestamp'))
        suffix = 'long' if side == 'LONG' else 'short'
        structure = state['evidence']['structure']
        for name in ('breakout_5m', 'breakdown_5m', f'retest_{suffix}_5m',
                     f'pullback_resume_{suffix}_5m', f'reclaim_retest_{suffix}_5m',
                     f'reclaim_sustained_{suffix}_5m'):
            if isinstance(features.get(name), bool):
                structure[name] = features[name]
        entry = _positive(plan.get('entry'))
        levels = [_positive(features.get(k)) for k in
                  (('resistance_5m','resistance_15m') if side == 'LONG' else ('support_5m','support_15m'))]
        available = [(level/entry-1)*(1 if side=='LONG' else -1) for level in levels if level and entry]
        available = [distance for distance in available if distance > 0]
        target_pct = trade['target_distance_pct']
        trade.update(_present(target_atr_1m=target_pct/(atr_fraction*100),
            target_range_60m=target_pct/(span*100) if span else None,
            next_obstacle_distance_pct=min(available)*100 if available else None,
            round_trip_fee_pct=pct(_finite(plan.get('round_trip_fee_rate')))))
        state['setup'] = {**guard, 'structure_timeframe_minutes':5,
            'structure_bar_open_at':features.get('structure_bar_open_5m'),
            'missing_fields':[key for key in ('structure_bar_open_5m','resistance_5m','support_5m')
                              if features.get(key) is None]}
        state['as_of'] = payload.get('timestamp')
        state['probability_status'] = 'UNCALIBRATED'
    return state
