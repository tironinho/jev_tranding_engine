from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.providers.openai.provider import _cost, _extract_json, _usage
from app.providers.openai.schemas import OpenAICallError, OpenAIInvalidSchema, OpenAINotConfigured

RESEARCH_PROMPT_VERSION = "evolution_researcher_v1"

RESEARCH_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "root_causes", "recommended_experiment", "risks", "confidence"],
    "properties": {
        "decision": {"type": "string", "enum": ["NO_ACTION", "PROPOSE_EXPERIMENT"]},
        "root_causes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "explanation", "evidence"],
                "properties": {
                    "code": {"type": "string"},
                    "explanation": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "recommended_experiment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["hypothesis", "target_component", "expected_effect", "allowed_changes"],
            "properties": {
                "hypothesis": {"type": "string"},
                "target_component": {"type": "string"},
                "expected_effect": {"type": "string"},
                "allowed_changes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}


@dataclass
class ResearchCompletion:
    request_id: str | None
    model: str
    output: dict[str, Any]
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None


class EvolutionResearchClient:
    """Research calls only. This client does not interpret a live market snapshot."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def complete(self, *, system_prompt: str, user_payload: dict[str, Any]) -> ResearchCompletion:
        if not self.settings.openai_api_key:
            raise OpenAINotConfigured("OPENAI_API_KEY is empty")
        if not self.settings.openai_research_model:
            raise OpenAINotConfigured("OPENAI_RESEARCH_MODEL is empty")
        if self.client is None:
            raise OpenAINotConfigured("research HTTP client is not available")
        body = {
            "model": self.settings.openai_research_model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_payload, default=str)}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "evolution_research",
                    "strict": True,
                    "schema": RESEARCH_JSON_SCHEMA,
                }
            },
        }
        started = time.perf_counter()
        response = await self.client.post(
            f"{self.settings.openai_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json=body,
            timeout=self.settings.openai_timeout_s,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code >= 400:
            raise OpenAICallError(f"openai_http_{response.status_code}")
        payload = response.json()
        try:
            parsed = _extract_json(payload)
        except OpenAIInvalidSchema:
            raise
        usage = _usage(payload)
        return ResearchCompletion(
            request_id=payload.get("id"),
            model=str(payload.get("model") or self.settings.openai_research_model),
            output=parsed,
            latency_ms=latency_ms,
            input_tokens=None if usage is None else usage["input_tokens"],
            output_tokens=None if usage is None else usage["output_tokens"],
            estimated_cost=_cost(self.settings, usage),
        )
