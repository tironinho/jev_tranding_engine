from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select

from app.db.models import (
    AnomalyRow,
    CodingAgentTaskRow,
    EngineEventRow,
    ExperimentRow,
    ResearchReportRow,
    StrategyVersionRow,
)
from app.evolution.schemas import Anomaly, Experiment, ResearchReport, StrategyVersion


@dataclass
class EvolutionSnapshot:
    versions: list[StrategyVersion] = field(default_factory=list)
    experiments: list[Experiment] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    reports: list[ResearchReport] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    controls: dict | None = None
    memory: list[dict] = field(default_factory=list)


class EvolutionRepository(Protocol):
    async def save_version(self, version: StrategyVersion) -> None: ...

    async def save_experiment(self, experiment: Experiment) -> None: ...

    async def save_anomaly(self, anomaly: Anomaly) -> None: ...

    async def save_report(self, report: ResearchReport) -> None: ...

    async def save_task(self, task: dict) -> None: ...

    async def save_controls(self, controls: dict) -> None: ...

    async def load(self) -> EvolutionSnapshot: ...


class MemoryEvolutionRepository:
    def __init__(self) -> None:
        self.snapshot = EvolutionSnapshot()

    async def save_version(self, version: StrategyVersion) -> None:
        self.snapshot.versions = [item for item in self.snapshot.versions if item.version != version.version]
        self.snapshot.versions.append(version)

    async def save_experiment(self, experiment: Experiment) -> None:
        self.snapshot.experiments = [item for item in self.snapshot.experiments if item.public_id != experiment.public_id]
        self.snapshot.experiments.append(experiment)
        self.snapshot.memory = [item for item in self.snapshot.memory if item.get("public_id") != experiment.public_id]
        self.snapshot.memory.append(
            {
                "public_id": experiment.public_id,
                "strategy_family": experiment.strategy_family.value,
                "hypothesis": experiment.hypothesis,
                "hypothesis_fingerprint": experiment.hypothesis_fingerprint,
                "status": experiment.status.value,
            }
        )

    async def save_anomaly(self, anomaly: Anomaly) -> None:
        self.snapshot.anomalies.append(anomaly)

    async def save_report(self, report: ResearchReport) -> None:
        self.snapshot.reports.append(report)

    async def save_task(self, task: dict) -> None:
        self.snapshot.tasks = [item for item in self.snapshot.tasks if item.get("task_id") != task.get("task_id")]
        self.snapshot.tasks.append(task)

    async def save_controls(self, controls: dict) -> None:
        self.snapshot.controls = dict(controls)

    async def load(self) -> EvolutionSnapshot:
        return self.snapshot


class PostgresEvolutionRepository:
    """Durable copy of evolution entities. Payloads are the pydantic JSON of the in-memory models."""

    def __init__(self, factory) -> None:
        self.factory = factory

    async def save_version(self, version: StrategyVersion) -> None:
        payload = version.model_dump(mode="json")
        await self._upsert(
            StrategyVersionRow(
                id=version.id,
                strategy_family=version.strategy_family.value,
                version=version.version,
                parent_version=version.parent_version,
                status=version.status.value,
                created_at=version.created_at,
                created_by=version.created_by,
                config_hash=version.config_hash,
                experiment_id=version.experiment_id,
                payload=payload,
            )
        )

    async def save_experiment(self, experiment: Experiment) -> None:
        await self._upsert(
            ExperimentRow(
                id=experiment.id,
                public_id=experiment.public_id,
                strategy_family=experiment.strategy_family.value,
                status=experiment.status.value,
                parent_version=experiment.parent_version,
                challenger_version=experiment.challenger_version,
                hypothesis_fingerprint=experiment.hypothesis_fingerprint,
                created_at=experiment.created_at,
                payload=experiment.model_dump(mode="json"),
            )
        )

    async def save_anomaly(self, anomaly: Anomaly) -> None:
        await self._upsert(
            AnomalyRow(
                id=anomaly.id,
                code=anomaly.code,
                strategy_family=anomaly.strategy_family.value,
                detected_at=anomaly.detected_at,
                payload=anomaly.model_dump(mode="json"),
            )
        )

    async def save_report(self, report: ResearchReport) -> None:
        await self._upsert(
            ResearchReportRow(
                id=report.id,
                decision=report.decision,
                prompt_version=report.prompt_version,
                created_at=report.created_at,
                payload=report.model_dump(mode="json"),
            )
        )

    async def save_task(self, task: dict) -> None:
        from uuid import UUID

        task_id = task.get("id") or task.get("task_id")
        await self._upsert(
            CodingAgentTaskRow(
                id=UUID(str(task_id)) if _is_uuid(task_id) else __import__("uuid").uuid4(),
                experiment_id=str(task.get("experiment_id") or task.get("task_id") or ""),
                provider=str(task.get("provider") or "mock"),
                status=str(task.get("status") or "recorded"),
                payload=task,
            )
        )

    async def save_controls(self, controls: dict) -> None:
        from datetime import datetime, timezone
        from uuid import uuid4

        await self._upsert(
            EngineEventRow(
                id=uuid4(),
                kind="evolution_controls",
                message="controls",
                timestamp=datetime.now(timezone.utc),
                payload=controls,
            )
        )

    async def load(self) -> EvolutionSnapshot:
        snapshot = EvolutionSnapshot()
        async with self.factory() as session:
            versions = (await session.execute(select(StrategyVersionRow))).scalars().all()
            experiments = (await session.execute(select(ExperimentRow))).scalars().all()
            anomalies = (await session.execute(select(AnomalyRow))).scalars().all()
            reports = (await session.execute(select(ResearchReportRow))).scalars().all()
            tasks = (await session.execute(select(CodingAgentTaskRow))).scalars().all()
            events = (
                await session.execute(select(EngineEventRow).where(EngineEventRow.kind == "evolution_controls"))
            ).scalars().all()
        snapshot.versions = [StrategyVersion.model_validate(row.payload) for row in versions]
        snapshot.experiments = [Experiment.model_validate(row.payload) for row in experiments]
        snapshot.anomalies = [Anomaly.model_validate(row.payload) for row in anomalies]
        snapshot.reports = [ResearchReport.model_validate(row.payload) for row in reports]
        snapshot.tasks = [row.payload for row in tasks]
        snapshot.memory = [
            {
                "public_id": item.public_id,
                "strategy_family": item.strategy_family.value,
                "hypothesis": item.hypothesis,
                "hypothesis_fingerprint": item.hypothesis_fingerprint,
                "status": item.status.value,
            }
            for item in snapshot.experiments
        ]
        if events:
            latest = max(events, key=lambda item: item.timestamp)
            snapshot.controls = dict(latest.payload)
        return snapshot

    async def _upsert(self, row) -> None:
        async with self.factory() as session:
            await session.merge(row)
            await session.commit()


def _is_uuid(value) -> bool:
    from uuid import UUID

    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True
