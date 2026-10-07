from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.evolution.experiment_memory import ExperimentMemory
from app.evolution.hypothesis_generator import accept_hypothesis
from app.evolution.paths import is_protected
from app.evolution.schemas import (
    DECISION_NO_ACTION,
    DECISION_PROPOSE,
    REJECTION_DUPLICATE,
    Anomaly,
    RecommendedExperiment,
    ResearchReport,
    RootCause,
)
from app.providers.openai.research import RESEARCH_PROMPT_VERSION, EvolutionResearchClient
from app.providers.openai.schemas import OpenAICallError, OpenAIInvalidSchema, OpenAINotConfigured

RESEARCH_PROMPT = """You are a research analyst for a quantitative trading experiment platform.
You do not place orders, change risk limits, request credentials, or run shell commands.
You do not write SQL and you do not edit code.
You propose at most one minimal, reversible, testable hypothesis.
If the evidence is weak, return decision NO_ACTION and an empty hypothesis.
Use only the structured anomaly and the similar-experiment list in the input.
allowed_changes must be repository paths. Never include risk, live execution, secrets, or kill switch.
"""


class EvolutionResearcher:
    """Structured research. A missing model or an invalid response never becomes a hypothesis."""

    def __init__(self, client: EvolutionResearchClient | None, *, model: str, api_key: str) -> None:
        self.client = client
        self.model = model
        self.api_key = api_key
        self.prompt_version = RESEARCH_PROMPT_VERSION

    def configured(self) -> bool:
        return bool(self.model and self.api_key and self.client is not None)

    async def analyze(self, anomaly: Anomaly | None, memory: ExperimentMemory) -> ResearchReport:
        now = datetime.now(timezone.utc)
        if anomaly is None:
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason="NO_ANOMALY",
                confidence=0,
            )
        similar = memory.find_similar(anomaly.strategy_family.value, anomaly.code)
        if similar is not None:
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason=REJECTION_DUPLICATE,
                anomaly_id=str(anomaly.id),
                evidence=[f"similar experiment {similar.get('public_id')}"],
                confidence=0,
            )
        if not self.model or not self.api_key:
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason="RESEARCH_MODEL_NOT_CONFIGURED",
                anomaly_id=str(anomaly.id),
                risks=["A hypothesis was not generated because no research model is configured."],
                confidence=0,
            )
        if self.client is None:
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason="RESEARCH_CLIENT_UNAVAILABLE",
                anomaly_id=str(anomaly.id),
                confidence=0,
            )
        payload = {
            "anomaly": anomaly.model_dump(mode="json"),
            "similar_experiments": [
                item
                for item in memory.records
                if item.get("strategy_family") == anomaly.strategy_family.value
            ][-20:],
        }
        try:
            completion = await self.client.complete(system_prompt=RESEARCH_PROMPT, user_payload=payload)
        except OpenAINotConfigured as exc:
            return _failed(now, anomaly, "RESEARCH_MODEL_NOT_CONFIGURED", str(exc))
        except (OpenAICallError, OpenAIInvalidSchema) as exc:
            return _failed(now, anomaly, "RESEARCH_CALL_FAILED", str(exc))
        except Exception as exc:
            return _failed(now, anomaly, "RESEARCH_CALL_FAILED", str(exc))
        try:
            parsed = _validated(completion.output)
        except (ValidationError, ValueError) as exc:
            return _failed(now, anomaly, "RESEARCH_INVALID_SCHEMA", str(exc), completion)
        hypothesis = parsed["recommended"].hypothesis.strip()
        blocked = [path for path in parsed["recommended"].allowed_changes if is_protected(path)]
        if parsed["decision"] != DECISION_PROPOSE or not accept_hypothesis(hypothesis) or blocked:
            reason = "NO_ACTION"
            if blocked:
                reason = "PROTECTED_CODE_CHANGED"
            elif parsed["decision"] == DECISION_PROPOSE:
                reason = "HYPOTHESIS_REJECTED"
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason=reason,
                anomaly_id=str(anomaly.id),
                root_causes=parsed["causes"],
                root_cause_hypotheses=[item.code for item in parsed["causes"]],
                risks=parsed["risks"],
                confidence=parsed["confidence"],
                model=completion.model,
                request_id=completion.request_id,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                latency_ms=completion.latency_ms,
                estimated_cost=completion.estimated_cost,
            )
        return ResearchReport(
            created_at=now,
            prompt_version=self.prompt_version,
            decision=DECISION_PROPOSE,
            reason=None,
            anomaly_id=str(anomaly.id),
            root_causes=parsed["causes"],
            root_cause_hypotheses=[item.code for item in parsed["causes"]],
            recommended_experiment=parsed["recommended"],
            recommended_experiments=[hypothesis],
            evidence=parsed["recommended"].allowed_changes,
            risks=parsed["risks"],
            confidence=parsed["confidence"],
            model=completion.model,
            request_id=completion.request_id,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=completion.latency_ms,
            estimated_cost=completion.estimated_cost,
        )


def _failed(now, anomaly: Anomaly, reason: str, error: str, completion=None) -> ResearchReport:
    return ResearchReport(
        created_at=now,
        prompt_version=RESEARCH_PROMPT_VERSION,
        decision=DECISION_NO_ACTION,
        reason=reason,
        anomaly_id=str(anomaly.id),
        error=error,
        confidence=0,
        model=None if completion is None else completion.model,
        request_id=None if completion is None else completion.request_id,
        latency_ms=None if completion is None else completion.latency_ms,
    )


def _validated(payload: dict[str, Any]) -> dict[str, Any]:
    decision = payload.get("decision")
    if decision not in {DECISION_NO_ACTION, DECISION_PROPOSE}:
        raise ValueError("decision")
    causes = [RootCause.model_validate(item) for item in payload.get("root_causes") or []]
    recommended = RecommendedExperiment.model_validate(payload.get("recommended_experiment") or {})
    confidence = float(payload.get("confidence") or 0)
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence")
    risks = [str(item) for item in payload.get("risks") or []]
    return {"decision": decision, "causes": causes, "recommended": recommended, "risks": risks, "confidence": confidence}
