from __future__ import annotations

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

_INSTRUCTIONS = {
    "trend_continuation_probability": (
        "The `baseline_action` side is the candidate from the weighted scores and `baseline_class`. "
        "It is not an order. Using trade_plan, that side reaches target before stop within horizon_minutes. "
        "Apply trade_plan.stop_policy: a tightened stop also terminates the trade. "
        "A timeout without reaching target is not a success."
    )
    + _ONCHAIN,
    "reversal_probability": "Price reverses against the `baseline_action` candidate." + _ONCHAIN,
    "false_breakout_probability": (
        "The break named in `baseline_class` is a false break. If the class has no break, stay near 0.5."
    )
    + _ONCHAIN,
}

_UNUSED_NOULS = {
    "buying_pressure_probability": 0.5,
    "selling_pressure_probability": 0.5,
    "volatility_expansion_probability": 0.5,
    "liquidity_sweep_probability": 0.5,
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
        body = {
            "model": self.model,
            "state": _state_for_model(payload, self.min_data_quality),
            "questions": {
                name: {"type": "noul", "instructions": _INSTRUCTIONS[name]}
                for name in _NOULS
            },
        }
        try:
            response = await self.client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
                timeout=self.timeout_s,
            )
        except Exception as exc:
            raise JevProviderError(str(exc)) from exc
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
                    "prompt_version": self.prompt_version,
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
        return {key: value for key, value in state.items() if key != "intelligence"}
    return state


def _normalized_state(payload: dict) -> dict[str, Any]:
    """Scores and unitless features only. Dollar prices stay off the request."""
    features = payload.get("features") or {}
    frac = features.get("range_60m_frac")
    state = {
        "symbol": payload.get("symbol"),
        "market_type": payload.get("market_type"),
        "baseline_action": payload.get("baseline_action"),
        "trade_plan": payload.get("trade_plan"),
        "baseline_confidence": payload.get("baseline_confidence"),
        "baseline_scores": payload.get("baseline_scores") or {},
        "baseline_class": payload.get("baseline_class"),
        "baseline_labels": payload.get("baseline_labels") or [],
        "taker_flow_1m": features.get("taker_flow_1m"),
        "return_60m": features.get("return_60m"),
        "setup_5m_return": features.get("setup_5m_return"),
        "context_15m_slope": features.get("context_15m_slope"),
        "price_vs_ema20_15m": features.get("price_vs_ema20_15m"),
        "range_60m": float(frac) if isinstance(frac, (int, float)) else None,
        "breakout": features.get("breakout"),
        "breakdown": features.get("breakdown"),
    }
    intelligence = jev_view(payload.get("intelligence"), payload.get("baseline_action"))
    if intelligence is not None:
        state["intelligence"] = intelligence
    return state


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
