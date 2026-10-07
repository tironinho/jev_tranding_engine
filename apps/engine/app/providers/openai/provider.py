from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.providers.openai.cache import DisabledMarketStateCache, MarketStateCache
from app.providers.openai.prompts import get_prompt, prompt_sha256
from app.providers.openai.schemas import (
    MARKET_STATE_JSON_SCHEMA,
    MarketState,
    OpenAICallError,
    OpenAIInvalidSchema,
    OpenAINotConfigured,
)
from app.resilience.guards import CircuitBreaker, TokenBucket


@dataclass
class OpenAICallRecord:
    request_id: str | None
    model: str
    prompt_version: str
    prompt_sha256: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None
    latency_ms: float
    token_usage: dict[str, int] | None
    estimated_cost: float | None
    error: str | None
    market_state: MarketState | None


class OpenAIProvider:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache: MarketStateCache | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.cache = cache or DisabledMarketStateCache()
        self.breaker = CircuitBreaker("openai", failure_threshold=4, recovery_seconds=60)
        self.bucket = TokenBucket(rate=1.0, capacity=4)

    def configured(self) -> bool:
        return bool(self.settings.openai_api_key)

    async def interpret(self, *, symbol: str, timestamp: str, features: dict[str, Any], context: dict[str, Any]) -> OpenAICallRecord:
        prompt_version = self.settings.openai_prompt_version
        prompt = get_prompt(prompt_version)
        digest = prompt_sha256(prompt_version)
        user_payload = {
            "symbol": symbol,
            "timestamp": timestamp,
            "features": features,
            "context": context,
        }
        if not self.configured():
            raise OpenAINotConfigured("OPENAI_API_KEY is empty")
        if not self.breaker.allow():
            raise OpenAICallError("OPENAI_CIRCUIT_OPEN")
        if self.client is None:
            raise OpenAINotConfigured("OpenAI HTTP client is not available")
        if not self.bucket.take():
            raise OpenAICallError("OPENAI_RATE_LIMIT")
        body = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(user_payload, default=str)}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "market_state",
                    "strict": True,
                    "schema": MARKET_STATE_JSON_SCHEMA,
                }
            },
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        response_json: dict | None = None
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"{self.settings.openai_base_url.rstrip('/')}/responses",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                    json=body,
                    timeout=self.settings.openai_timeout_s,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                self.breaker.record_failure()
                break
            except httpx.HTTPError as exc:
                last_error = exc
                self.breaker.record_failure()
                break
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                self.breaker.record_failure()
                await _async_sleep(0.4 * (2**attempt))
                continue
            if response.status_code >= 400:
                self.breaker.record_failure()
                raise OpenAICallError(f"openai_http_{response.status_code}")
            response_json = response.json()
            self.breaker.record_success()
            break
        latency_ms = (time.perf_counter() - started) * 1000
        if response_json is None:
            raise OpenAICallError(str(last_error or "openai_failed"))
        try:
            parsed = _extract_json(response_json)
            state = MarketState.model_validate(parsed)
            if not state.evidence_is_bounded():
                raise OpenAIInvalidSchema("evidence item length")
        except OpenAIInvalidSchema:
            raise
        except Exception as exc:
            raise OpenAIInvalidSchema(str(exc)) from exc
        usage = _usage(response_json)
        return OpenAICallRecord(
            request_id=response_json.get("id"),
            model=str(response_json.get("model") or self.settings.openai_model),
            prompt_version=prompt_version,
            prompt_sha256=digest,
            input_payload=user_payload,
            output_payload=state.model_dump(),
            latency_ms=latency_ms,
            token_usage=usage,
            estimated_cost=_cost(self.settings, usage),
            error=None,
            market_state=state,
        )


def _extract_json(payload: dict) -> dict:
    if isinstance(payload.get("output_parsed"), dict):
        return _coerce_market_state(payload["output_parsed"])
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part.get("json"), dict):
                return _coerce_market_state(part["json"])
            elif isinstance(part.get("parsed"), dict):
                return _coerce_market_state(part["parsed"])
    if not texts:
        raise OpenAIInvalidSchema("missing output_text")
    try:
        loaded = json.loads("".join(texts))
    except json.JSONDecodeError as exc:
        raise OpenAIInvalidSchema("output is not json") from exc
    if not isinstance(loaded, dict):
        raise OpenAIInvalidSchema("output is not an object")
    return _coerce_market_state(loaded)


def _coerce_market_state(payload: dict) -> dict:
    """Keep a 0–1 score and a short evidence line inside the contract the model was given."""
    numeric = (
        "trend_strength",
        "momentum_quality",
        "breakout_quality",
        "overextension",
        "reversal_risk",
        "anomaly_score",
        "confidence",
    )
    coerced = dict(payload)
    for name in numeric:
        value = coerced.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            coerced[name] = max(0.0, min(1.0, float(value)))
    evidence = coerced.get("evidence")
    if isinstance(evidence, list):
        lines = [str(item).strip()[:160] for item in evidence if str(item).strip()]
        coerced["evidence"] = lines[:8] or ["snapshot"]
    return coerced


def _usage(payload: dict) -> dict[str, int] | None:
    usage = payload.get("usage") or {}
    if not usage:
        return None
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _cost(settings: Settings, usage: dict[str, int] | None) -> float | None:
    if not usage or settings.openai_input_usd_per_1m is None or settings.openai_output_usd_per_1m is None:
        return None
    return (
        usage["input_tokens"] / 1_000_000 * settings.openai_input_usd_per_1m
        + usage["output_tokens"] / 1_000_000 * settings.openai_output_usd_per_1m
    )


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
