from __future__ import annotations

import time
from typing import Any

from app.providers.jev.schemas import JevAssessment, JevMarketRequest, JevProviderError


class RealJevProvider:
    """Calls only the operator-supplied `JEV_BASE_URL`. No host is built in.

    POST the `JevMarketRequest` JSON with `Authorization: Bearer <JEV_API_KEY>`.
    The body that comes back must be a JSON object with the probability fields
    of `JevAssessment`, or the same object nested under `assessment`.
    """

    provider_name = "real"
    provider_version = "real-http-v1"

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
        self._api_key = api_key
        self.api_key_configured = bool(api_key)
        self.model = model or None
        self.timeout_s = timeout_s
        self.client = client

    async def evaluate_market_state(self, request: JevMarketRequest) -> JevAssessment:
        if not self.base_url or not self._api_key:
            raise JevProviderError("JEV_BASE_URL or JEV_API_KEY is empty")
        if self.client is None:
            raise JevProviderError("Jev HTTP client is not available")
        started = time.perf_counter()
        try:
            response = await self.client.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=request.model_dump(mode="json"),
                timeout=self.timeout_s,
            )
        except Exception as exc:
            raise JevProviderError(str(exc)) from exc
        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise JevProviderError(f"jev_http_{status}")
        try:
            body = response.json()
        except Exception as exc:
            raise JevProviderError("jev response is not json") from exc
        payload = _assessment_payload(body)
        try:
            assessment = JevAssessment.model_validate(
                {
                    **payload,
                    "provider": "real",
                    "is_mock": False,
                    "provider_version": self.provider_version,
                    "prompt_version": self.prompt_version,
                    "model": payload.get("model") or self.model,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "error": None,
                }
            )
        except Exception as exc:
            raise JevProviderError(str(exc)) from exc
        return assessment


def _assessment_payload(body: object) -> dict:
    if not isinstance(body, dict):
        raise JevProviderError("jev response is not an object")
    nested = body.get("assessment")
    if isinstance(nested, dict) and "trend_continuation_probability" in nested:
        return nested
    return body
