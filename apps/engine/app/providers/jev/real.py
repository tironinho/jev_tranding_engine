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
    )
    + _ONCHAIN
    + _BINANCE_AGENT,
    "reversal_probability": "Price reverses against the `baseline_action` candidate." + _ONCHAIN + _BINANCE_AGENT,
    "false_breakout_probability": (
        "The break or reclaim named in `baseline_class` fails and returns through its structural level. "
        "If the class has no break or reclaim, stay near 0.5."
    )
    + _ONCHAIN
    + _BINANCE_AGENT,
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
    ) + _ONCHAIN,
    "reversal_probability": "Price reverses against the `baseline_action` candidate." + _ONCHAIN,
    "false_breakout_probability": (
        "The break named in `baseline_class` is a false break. If the class has no break, stay near 0.5."
    ) + _ONCHAIN,
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
                    "one requires extension beyond the observed hour range; do not assume it will happen. "
                    "Net R:R and break_even_probability describe geometry, not evidence of success."
                ) + _ONCHAIN}
        body = {
            "model": self.model,
            "state": _state_for_model(payload, self.min_data_quality),
            "questions": {
                name: {"type": "noul", "instructions": instructions[name]}
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
    """A context below the quality floor stays on the desk and out of the question.

    Incomplete external fields were pulling continuation under the veto line.
    """
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
                         baseline_labels=payload.get("baseline_labels") or [])
            plan = payload.get("trade_plan")
            if isinstance(plan, dict):
                state["trade_plan"] = _normalized_plan(plan, features, frac)
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


def _normalized_plan(plan: dict, features: dict, frac: float | None) -> dict:
    entry = _positive(plan.get("entry"))
    stop = _positive(plan.get("stop"))
    target = _positive(plan.get("target"))
    stop_distance = abs(entry - stop) / entry if entry and stop else None
    target_distance = abs(target - entry) / entry if entry and target else None
    atr_fraction = _positive(features.get("atr_normalized"))
    return {
        "gross_rr": plan.get("gross_rr"),
        "net_rr": plan.get("net_rr"),
        "break_even_probability": plan.get("break_even_probability"),
        "stop_distance_fraction": stop_distance,
        "target_distance_fraction": target_distance,
        "stop_atr_multiple": stop_distance / atr_fraction if stop_distance is not None and atr_fraction else None,
        "target_atr_multiple": target_distance / atr_fraction if target_distance is not None and atr_fraction else None,
        "target_range_multiple": target_distance / frac if target_distance is not None and frac else None,
        "horizon_minutes": plan.get("horizon_minutes"),
        "round_trip_fee_rate": plan.get("round_trip_fee_rate"),
        "stress_bps": plan.get("stress_bps"),
        "requires_structure_break": plan.get("requires_structure_break"),
        **{key: plan[key] for key in ("target_extended_for_costs", "target_policy") if key in plan},
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
