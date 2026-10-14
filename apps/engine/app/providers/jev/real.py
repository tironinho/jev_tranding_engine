from __future__ import annotations

import math
import time
from typing import Any

from app.intelligence.context import jev_view
from app.providers.jev.schemas import JevAssessment, JevMarketRequest, JevProviderError

_NOULS = (
    "trend_continuation_probability",
    "reversal_probability",
    "false_breakout_probability",
)

_ONCHAIN = (
    " onchain.support is oriented to baseline_action: positive supports that side. "
    "onchain.class is SUPPORTIVE, NEUTRAL, HOSTILE, or UNKNOWN. "
    "UNKNOWN means on-chain does not vote and must not move the answer. "
    "onchain.evidence is the share of on-chain weight that had data. "
    "A missing field is unknown, not zero. valuation_stretch and activity are not a side."
)

_BINANCE_AGENT = (
    " binance_agent is a read-only deterministic grouping of the same Binance market fields. "
    "It is not an order or independent evidence, so do not count it twice."
)

_INSTRUCTIONS = {
    "trend_continuation_probability": (
        "The `baseline_action` side is the candidate from the weighted scores and `baseline_class`. "
        "It is not an order. When `trade_plan` is present, estimate the probability that price reaches that "
        "plan's target before its stop within `horizon_minutes`. Use the supplied net R:R and break-even "
        "probability only as geometry, never as evidence that the outcome is more likely. Account for target "
        "distance, stop distance, trend, momentum, order flow, break or reclaim quality, and market context. "
        "When no trade_plan is present, estimate directional follow-through over the stated horizon."
    ),
    "reversal_probability": "Price reverses against the `baseline_action` candidate.",
    "false_breakout_probability": (
        "The break or reclaim named in `baseline_class` fails and returns through its structural level. "
        "If the class has no break or reclaim, stay near 0.5."
    ),
}

_UNUSED_NOULS = {
    "buying_pressure_probability": 0.5,
    "selling_pressure_probability": 0.5,
    "volatility_expansion_probability": 0.5,
    "liquidity_sweep_probability": 0.5,
}

_INITIAL_LIVE_INSTRUCTIONS = {
    "trend_continuation_probability": "The `baseline_action` side reaches the target before the stop.",
    "reversal_probability": "Price reverses against `baseline_action`.",
    "false_breakout_probability": "The `breakout` or `breakdown` flag is a false break.",
}

_FIRST_TEN_INSTRUCTIONS = {
    "trend_continuation_probability": (
        "The `baseline_action` side is the candidate from the weighted scores and `baseline_class`. "
        "It is not an order. That side reaches the target before the stop."
    ),
    "reversal_probability": "Price reverses against the `baseline_action` candidate.",
    "false_breakout_probability": (
        "The break_event (BREAKOUT or BREAKDOWN) fails and returns through its structural level. "
        "When break_event is NONE or UNKNOWN, stay near 0.5. "
        "baseline_class describes alignment, not the structural event."
    ),
}


class RealJevProvider:
    """POST a TypeSafe System One request to the configured URL. No host is built in.

    The body is `model`, a small normalized `state`, and three Noul questions.
    Each `answers.<name>.noul` is copied into `JevAssessment`. The veto stays in our code.
    """

    provider_name = "real"
    provider_version = "typesafe-systemone-v1"

    def __init__(
        self,
        *,
        prompt_version: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 3.0,
        min_data_quality: float = 0.70,
        client: Any | None = None,
    ) -> None:
        self.prompt_version = prompt_version
        self.base_url = base_url.strip().rstrip("/")
        self._api_key = "".join(api_key.split())
        self.api_key_configured = bool(self._api_key)
        self.model = model.strip() if model else "jev-latest"
        self.timeout_s = timeout_s
        self.min_data_quality = min_data_quality
        self.client = client

    async def evaluate_market_state(self, request: JevMarketRequest) -> JevAssessment:
        if not self.base_url or not self._api_key:
            raise JevProviderError("JEV_BASE_URL or JEV_API_KEY is empty")
        if self.client is None:
            raise JevProviderError("Jev HTTP client is not available")
        started = time.perf_counter()
        payload = request.model_dump(mode="json")
        instructions = {"initial_live_2026_10_08": _INITIAL_LIVE_INSTRUCTIONS,
                        "live_first_ten_2026_10_09": _FIRST_TEN_INSTRUCTIONS}.get(request.evaluation_profile, _INSTRUCTIONS)
        if request.evaluation_profile == "live_first_ten_2026_10_09" and request.trade_plan:
            instructions = {**_FIRST_TEN_INSTRUCTIONS,
                "trend_continuation_probability": (
                    "Estimate the probability that the baseline_action candidate reaches the supplied "
                    "trade_plan target before its stop within horizon_minutes. Use target and stop "
                    "distances, trend, momentum, flow, and break quality. A target_range_multiple above "
                    "one requires extension beyond the observed reference range; do not assume it will happen. "
                    "Net R:R and break_even_probability describe geometry, not evidence of success."
                    " market_context contains recent returns, trend alignment, volume and actual order flow. "
                    "Use these observations once; baseline_scores summarize the same evidence. "
                    "Missing observations are unknown, not bearish. Estimate the supplied plan honestly; "
                    "do not raise probability to meet an entry threshold."
                )}
        state = _state_for_model(payload, self.min_data_quality)
        # Describe only external evidence that survived normalization and the
        # quality gate. Missing providers must not add instructions or votes.
        intelligence = state.get("intelligence") or {}
        extra = ""
        if request.evaluation_profile != "initial_live_2026_10_08":
            if isinstance(intelligence.get("onchain"), dict) and intelligence["onchain"]:
                extra += _ONCHAIN
            if isinstance(intelligence.get("binance_agent"), dict) and intelligence["binance_agent"]:
                extra += _BINANCE_AGENT
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                name: {"type": "noul", "instructions": instructions[name] + extra}
                for name in _NOULS
            },
        }
        try:
            response = await self.client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
                timeout=request.evaluation_timeout_s or self.timeout_s,
            )
        except Exception as exc:
            error_type = type(exc).__name__.lower()
            if "timeout" in error_type:
                reason = "jev_timeout"
            elif "connect" in error_type or "network" in error_type:
                reason = "jev_connection_error"
            else:
                reason = f"jev_transport_{type(exc).__name__}"
            raise JevProviderError(reason) from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise JevProviderError(f"jev_http_{status}")
        try:
            parsed = response.json()
        except Exception as exc:
            raise JevProviderError("jev response is not json") from exc
        probabilities = _noul_probabilities(parsed)
        try:
            return JevAssessment.model_validate(
                {
                    **_UNUSED_NOULS,
                    **probabilities,
                    "provider": "real",
                    "is_mock": False,
                    "provider_version": self.provider_version,
                    "prompt_version": (request.prompt_version
                                       if request.evaluation_profile != "plan_outcome_v3" else self.prompt_version),
                    "model": parsed.get("model") if isinstance(parsed, dict) else self.model,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "error": None,
                    "sent_request": body,
                }
            )
        except Exception as exc:
            raise JevProviderError(str(exc)) from exc


def _state_for_model(payload: dict, min_quality: float) -> dict[str, Any]:
    """Keep only usable evidence; omitted feeds do not add questions or votes."""
    state = _normalized_state(payload)
    intelligence = state.get("intelligence")
    if not isinstance(intelligence, dict):
        return state
    quality = intelligence.get("data_quality")
    overall = quality.get("overall") if isinstance(quality, dict) else None
    if isinstance(overall, (int, float)) and overall < min_quality:
        if payload.get("evaluation_profile") == "live_first_ten_2026_10_09":
            return {key: value for key, value in state.items() if key != "intelligence"}
        # External feeds can be thin while the Binance snapshot is complete.
        # Keep only high-quality exchange-native summaries instead of dropping
        # useful book/trade evidence together with missing third-party data.
        retained = _exchange_native_intelligence(intelligence, min_quality)
        if retained:
            state["intelligence"] = retained
            return state
        return {key: value for key, value in state.items() if key != "intelligence"}
    intelligence = {key: value for key, value in intelligence.items() if value is not None}
    onchain = intelligence.get("onchain")
    if isinstance(onchain, dict):
        quality = _finite(onchain.get("quality"))
        evidence = _finite(onchain.get("evidence"))
        if (onchain.get("class") == "UNKNOWN" or _finite(onchain.get("support")) is None
                or evidence is None or evidence < .5 or (quality is not None and quality < min_quality)):
            intelligence.pop("onchain", None)
            # Removing the block but retaining its missing-field list still
            # sends the same unknown observations indirectly to the model.
            if isinstance(intelligence.get("data_quality"), dict):
                quality_view = dict(intelligence['data_quality'])
                quality_view['categories'] = {k:v for k,v in (quality_view.get('categories') or {}).items() if k != 'onchain'}
                quality_view['missing'] = [name for name in quality_view.get('missing') or [] if not str(name).startswith('onchain.')]
                intelligence['data_quality'] = quality_view
    if any(key != 'data_quality' for key in intelligence):
        state['intelligence'] = intelligence
    else:
        state.pop('intelligence', None)
    return state


def _exchange_native_intelligence(intelligence: dict, min_quality: float) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    agent = intelligence.get("binance_agent")
    if isinstance(agent, dict) and _quality(agent) >= min_quality:
        kept["binance_agent"] = agent
    micro = intelligence.get("microstructure")
    if isinstance(micro, dict) and _quality(micro) >= min_quality:
        kept["microstructure"] = micro
    if kept:
        kept["data_quality"] = {
            "scope": "exchange_native_only",
            "overall": min((_quality(value) for key, value in kept.items() if key != "data_quality"), default=0.0),
        }
    return kept


def _quality(value: dict) -> float:
    raw = value.get("quality")
    return float(raw) if isinstance(raw, (int, float)) and math.isfinite(raw) else 0.0


def _normalized_state(payload: dict) -> dict[str, Any]:
    """Scores and unitless features only. Dollar prices stay off the request."""
    features = payload.get("features") or {}
    frac = _finite(features.get("range_60m_frac"))
    if payload.get("evaluation_profile") in {"initial_live_2026_10_08", "live_first_ten_2026_10_09"}:
        state = {
            "symbol": payload.get("symbol"),
            "market_type": payload.get("market_type"),
            "baseline_action": payload.get("baseline_action"),
            "baseline_confidence": payload.get("baseline_confidence"),
            "baseline_scores": payload.get("baseline_scores") or {},
            "taker_flow_1m": features.get("taker_flow_1m"),
            "return_60m": features.get("return_60m"),
            "range_60m": frac,
            "breakout": features.get("breakout"),
            "breakdown": features.get("breakdown"),
        }
        first_ten = payload.get("evaluation_profile") == "live_first_ten_2026_10_09"
        if first_ten:
            state.update(baseline_class=payload.get("baseline_class"),
                         baseline_labels=payload.get("baseline_labels") or [],
                         break_event=_break_event(features, payload.get("baseline_labels") or []))
            if payload.get("candidate_origin"):
                state.update(candidate_origin=payload["candidate_origin"],
                             baseline_threshold=payload.get("baseline_threshold"),
                             candidate_threshold=payload.get("candidate_threshold"))
            # Compact exchange observations survive missing external providers.
            # No duplicated agent opinions or large third-party payloads.
            state["market_context"] = {
                key: _finite(features.get(key)) for key in (
                    "return_5m", "return_15m", "ema_alignment", "context_15m_slope",
                    "rsi", "volume_ratio", "taker_flow_5m", "imbalance_10",
                    "range_position", "spread_bps",
                )
            }
            plan = payload.get("trade_plan")
            if isinstance(plan, dict):
                state["trade_plan"] = _normalized_plan(plan, features, frac)
                if plan.get("target_policy") == "market_range_v1":
                    state["market_context"]["return_240m"] = _finite(features.get("return_240m"))
        intelligence = (jev_view(payload.get("intelligence"), payload.get("baseline_action"))
                        if first_ten else payload.get("intelligence"))
        if isinstance(intelligence, dict):
            if first_ten:
                intelligence = {key: value for key, value in intelligence.items()
                                if key not in {"binance_agent", "microstructure"}}
            state["intelligence"] = intelligence
        return state
    state = {
        "symbol": payload.get("symbol"),
        "market_type": payload.get("market_type"),
        "baseline_action": payload.get("baseline_action"),
        "baseline_confidence": payload.get("baseline_confidence"),
        "baseline_scores": payload.get("baseline_scores") or {},
        "baseline_class": payload.get("baseline_class"),
        "baseline_labels": payload.get("baseline_labels") or [],
        "return_1m": _finite(features.get("return_1m")),
        "return_5m": _finite(features.get("return_5m")),
        "return_15m": _finite(features.get("return_15m")),
        "return_60m": _finite(features.get("return_60m")),
        "setup_5m_return": _finite(features.get("setup_5m_return")),
        "context_15m_slope": _finite(features.get("context_15m_slope")),
        "price_vs_ema20_15m": _finite(features.get("price_vs_ema20_15m")),
        "rsi": _finite(features.get("rsi")),
        "roc": _finite(features.get("roc")),
        "atr_normalized": _finite(features.get("atr_normalized")),
        "realized_volatility": _finite(features.get("realized_volatility")),
        "volume_ratio": _finite(features.get("volume_ratio")),
        "volume_zscore": _finite(features.get("volume_zscore")),
        "taker_flow_1m": _finite(features.get("taker_flow_1m")),
        "taker_flow_5m": _finite(features.get("taker_flow_5m")),
        "orderflow_delta_ratio": _finite(features.get("orderflow_delta_ratio")),
        "book_imbalance_5": _finite(features.get("imbalance_5")),
        "book_imbalance_10": _finite(features.get("imbalance_10")),
        "book_imbalance_20": _finite(features.get("imbalance_20")),
        "spread_bps": _finite(features.get("spread_bps")),
        "range_position": _finite(features.get("range_position")),
        "distance_to_vwap": _finite(features.get("distance_to_vwap")),
        "range_60m": frac,
        "breakout": features.get("breakout"),
        "breakdown": features.get("breakdown"),
        "bullish_reclaim": features.get("bullish_reclaim"),
        "bearish_reclaim": features.get("bearish_reclaim"),
    }
    plan = payload.get("trade_plan")
    if isinstance(plan, dict):
        # Ratios and time are enough to assess the outcome. Absolute prices stay
        # out of the provider request.
        state["trade_plan"] = _normalized_plan(plan, features, frac)
    intelligence = jev_view(payload.get("intelligence"), payload.get("baseline_action"))
    if intelligence is not None:
        state["intelligence"] = intelligence
    return state


def _break_event(features: dict, labels: list[str]) -> str:
    """Alignment can be CLASS_ALIGNED while the structural event is BREAKDOWN."""
    up = features.get("breakout") is True or "BREAKOUT" in labels
    down = features.get("breakdown") is True or "BREAKDOWN" in labels
    if up and down:
        return "UNKNOWN"
    if up:
        return "BREAKOUT"
    if down:
        return "BREAKDOWN"
    return "NONE" if features.get("breakout") is False and features.get("breakdown") is False else "UNKNOWN"


def _normalized_plan(plan: dict, features: dict, frac: float | None) -> dict:
    entry = _positive(plan.get("entry"))
    stop = _positive(plan.get("stop"))
    target = _positive(plan.get("target"))
    stop_distance = abs(entry - stop) / entry if entry and stop else None
    target_distance = abs(target - entry) / entry if entry and target else None
    atr_fraction = _positive(features.get("atr_normalized"))
    reference_fraction = _positive(plan.get("reference_range_fraction")) or frac
    return {
        "gross_rr": plan.get("gross_rr"),
        "net_rr": plan.get("net_rr"),
        "break_even_probability": plan.get("break_even_probability"),
        "stop_distance_fraction": stop_distance,
        "target_distance_fraction": target_distance,
        "stop_atr_multiple": stop_distance / atr_fraction if stop_distance is not None and atr_fraction else None,
        "target_atr_multiple": target_distance / atr_fraction if target_distance is not None and atr_fraction else None,
        "target_range_multiple": target_distance / reference_fraction if target_distance is not None and reference_fraction else None,
        "horizon_minutes": plan.get("horizon_minutes"),
        "round_trip_fee_rate": plan.get("round_trip_fee_rate"),
        "stress_bps": plan.get("stress_bps"),
        "requires_structure_break": plan.get("requires_structure_break"),
        **{key: plan[key] for key in ("target_extended_for_costs", "target_policy",
                                    "reference_range_minutes", "max_hold_minutes") if key in plan},
    }


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive(value: object) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _noul_probabilities(body: object) -> dict[str, float]:
    if not isinstance(body, dict):
        raise JevProviderError("jev response is not an object")
    answers = body.get("answers")
    if not isinstance(answers, dict):
        raise JevProviderError("jev response has no answers")
    values: dict[str, float] = {}
    for name in _NOULS:
        answer = answers.get(name)
        if not isinstance(answer, dict) or not isinstance(answer.get("noul"), (int, float)):
            raise JevProviderError(f"missing noul {name}")
        values[name] = float(answer["noul"])
    return values
