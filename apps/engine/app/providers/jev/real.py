from __future__ import annotations

import time
from typing import Any

from app.providers.jev.schemas import JevAssessment, JevMarketRequest, JevProviderError

_NOULS = (
    "trend_continuation_probability",
    "reversal_probability",
    "false_breakout_probability",
)

_INSTRUCTIONS = {
    "trend_continuation_probability": "The `baseline_action` side reaches the target before the stop.",
    "reversal_probability": "Price reverses against `baseline_action`.",
    "false_breakout_probability": "The `breakout` or `breakdown` flag is a false break.",
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
        client: Any | None = None,
    ) -> None:
        self.prompt_version = prompt_version
        self.base_url = base_url.strip().rstrip("/")
        self._api_key = "".join(api_key.split())
        self.api_key_configured = bool(self._api_key)
        self.model = model.strip() if model else "jev-latest"
        self.timeout_s = timeout_s
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
            "state": _normalized_state(payload),
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
                }
            )
        except Exception as exc:
            raise JevProviderError(str(exc)) from exc


def _normalized_state(payload: dict) -> dict[str, Any]:
    """Scores and unitless features only. Dollar prices stay off the request."""
    features = payload.get("features") or {}
    frac = features.get("range_60m_frac")
    return {
        "symbol": payload.get("symbol"),
        "market_type": payload.get("market_type"),
        "baseline_action": payload.get("baseline_action"),
        "baseline_confidence": payload.get("baseline_confidence"),
        "baseline_scores": payload.get("baseline_scores") or {},
        "taker_flow_1m": features.get("taker_flow_1m"),
        "return_60m": features.get("return_60m"),
        "range_60m": float(frac) if isinstance(frac, (int, float)) else None,
        "breakout": features.get("breakout"),
        "breakdown": features.get("breakdown"),
    }


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
