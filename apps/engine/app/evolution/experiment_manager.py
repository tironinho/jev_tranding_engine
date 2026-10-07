from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.evolution.experiment_memory import ExperimentMemory, hypothesis_fingerprint
from app.evolution.schemas import (
    REJECTION_CANCELLED,
    REJECTION_DUPLICATE,
    Experiment,
    ExperimentStatus,
    StrategyFamily,
)


class ExperimentManager:
    def __init__(self, memory: ExperimentMemory) -> None:
        self.memory = memory
        self.experiments: dict[str, Experiment] = {}
        self._sequence = 0

    def next_public_id(self) -> str:
        self._sequence += 1
        return f"EXP-{self._sequence:06d}"

    def create(
        self,
        *,
        family: StrategyFamily,
        parent_version: str,
        hypothesis: str,
        problem: str,
        dataset_version: str,
        seed: int,
    ) -> Experiment | str:
        if self.memory.find_similar(family.value, hypothesis) is not None:
            return REJECTION_DUPLICATE
        now = datetime.now(timezone.utc)
        public_id = self.next_public_id()
        experiment = Experiment(
            public_id=public_id,
            strategy_family=family,
            parent_version=parent_version,
            hypothesis=hypothesis,
            problem_statement=problem,
            created_at=now,
            status=ExperimentStatus.PROPOSED,
            hypothesis_fingerprint=hypothesis_fingerprint(family.value, hypothesis),
            git_branch=f"experiment/{public_id}",
            dataset_version=dataset_version,
            seed=seed,
            timeline=[{"at": now.isoformat(), "event": "proposed"}],
        )
        self.experiments[public_id] = experiment
        self.memory.remember(
            {
                "public_id": public_id,
                "strategy_family": family.value,
                "hypothesis": hypothesis,
                "hypothesis_fingerprint": experiment.hypothesis_fingerprint,
                "status": experiment.status.value,
            }
        )
        return experiment

    def get(self, public_id: str) -> Experiment | None:
        return self.experiments.get(public_id)

    def cancel(self, public_id: str) -> Experiment | None:
        experiment = self.experiments.get(public_id)
        if experiment is None or experiment.status in {ExperimentStatus.PROMOTED, ExperimentStatus.REJECTED}:
            return experiment
        experiment.status = ExperimentStatus.FAILED
        experiment.rejection_reason = REJECTION_CANCELLED
        experiment.finished_at = datetime.now(timezone.utc)
        experiment.timeline.append({"at": experiment.finished_at.isoformat(), "event": "cancelled"})
        return experiment

    def reject(self, public_id: str, reason: str) -> Experiment | None:
        experiment = self.experiments.get(public_id)
        if experiment is None:
            return None
        experiment.status = ExperimentStatus.REJECTED
        experiment.rejection_reason = reason
        experiment.decision = "REJECT"
        experiment.finished_at = datetime.now(timezone.utc)
        experiment.timeline.append({"at": experiment.finished_at.isoformat(), "event": "rejected", "reason": reason})
        return experiment

    def note(self, public_id: str, event: str, payload: dict | None = None) -> None:
        experiment = self.experiments[public_id]
        experiment.timeline.append({"at": datetime.now(timezone.utc).isoformat(), "event": event, "payload": payload or {}})


def new_dataset_version() -> str:
    return f"dataset-{uuid4().hex[:12]}"
