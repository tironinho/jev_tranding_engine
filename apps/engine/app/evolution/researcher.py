from __future__ import annotations

from datetime import datetime, timezone

from app.evolution.experiment_memory import ExperimentMemory
from app.evolution.schemas import DECISION_NO_ACTION, REJECTION_DUPLICATE, Anomaly, ResearchReport

RESEARCH_PROMPT_VERSION = "evolution_researcher_v1"
RESEARCH_PROMPT = """You are a research analyst for a quantitative trading experiment platform.
You do not place orders, change risk limits, request credentials, or run shell commands.
You do not write SQL.
You propose at most one minimal, reversible, testable hypothesis.
If the evidence is weak or the experiment was already tried, return no recommended experiment.
Use only the structured anomaly and the similar-experiment list in the input.
"""


class EvolutionResearcher:
    """Structured research. Without a configured research model this module refuses to invent a hypothesis."""

    def __init__(self, *, model: str, enabled: bool) -> None:
        self.model = model
        self.enabled = enabled
        self.prompt_version = RESEARCH_PROMPT_VERSION

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
        if not self.enabled or not self.model:
            return ResearchReport(
                created_at=now,
                prompt_version=self.prompt_version,
                decision=DECISION_NO_ACTION,
                reason="RESEARCH_MODEL_NOT_CONFIGURED",
                anomaly_id=str(anomaly.id),
                risks=["A hypothesis was not generated because no research model is configured."],
                confidence=0,
            )
        return ResearchReport(
            created_at=now,
            prompt_version=self.prompt_version,
            decision=DECISION_NO_ACTION,
            reason="RESEARCH_CALL_NOT_ENABLED_IN_THIS_VERSION",
            anomaly_id=str(anomaly.id),
            confidence=0,
        )
