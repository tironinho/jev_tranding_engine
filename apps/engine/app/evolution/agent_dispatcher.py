from __future__ import annotations

from typing import Protocol

from app.evolution.paths import ALLOWED_PATHS, PROTECTED_PATHS
from app.evolution.schemas import Experiment


class CodingAgentNotConfigured(Exception):
    pass


class CodingAgentProvider(Protocol):
    name: str

    async def create_experiment_task(self, task: dict) -> dict: ...

    async def get_task_status(self, task_id: str) -> dict: ...

    async def get_result(self, task_id: str) -> dict: ...


class MockCodingAgentProvider:
    """Records the task and does not touch the repository."""

    name = "mock"

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}

    async def create_experiment_task(self, task: dict) -> dict:
        stored = {**task, "provider": self.name, "status": "recorded", "repository_modified": False}
        self.tasks[task["task_id"]] = stored
        return stored

    async def get_task_status(self, task_id: str) -> dict:
        return self.tasks.get(task_id, {"task_id": task_id, "status": "missing"})

    async def get_result(self, task_id: str) -> dict:
        found = self.tasks.get(task_id)
        if found is None:
            return {"task_id": task_id, "status": "missing"}
        return {**found, "diff": None, "tests": None}


class CursorCodingAgentProvider:
    """Stub. No Cursor endpoint is called until official CLI or API docs are supplied."""

    name = "cursor"

    async def create_experiment_task(self, task: dict) -> dict:
        raise CodingAgentNotConfigured(
            "CursorCodingAgentProvider has no client. "
            "Official Cursor CLI/Cloud Agent documentation was not wired, so no command or endpoint was invented."
        )

    async def get_task_status(self, task_id: str) -> dict:
        raise CodingAgentNotConfigured("Cursor coding agent is not configured")

    async def get_result(self, task_id: str) -> dict:
        raise CodingAgentNotConfigured("Cursor coding agent is not configured")


def build_provider(name: str) -> CodingAgentProvider:
    if name == "cursor":
        return CursorCodingAgentProvider()
    return MockCodingAgentProvider()


def build_task_prompt(experiment: Experiment) -> str:
    allowed = "\n".join(f"- {path}" for path in ALLOWED_PATHS)
    protected = "\n".join(f"- {path}" for path in PROTECTED_PATHS)
    branch = experiment.git_branch or f"experiment/{experiment.public_id}"
    return (
        f"You are implementing experiment {experiment.public_id}.\n\n"
        f"Problem:\n{experiment.problem_statement}\n\n"
        f"Hypothesis:\n{experiment.hypothesis}\n\n"
        f"Parent:\n{experiment.parent_version}\n"
        f"Challenger:\n{experiment.challenger_version or 'unassigned'}\n"
        f"Branch:\n{branch}\n\n"
        "Do not modify main directly.\n"
        "Allowed changes:\n"
        f"{allowed}\n\n"
        "Protected paths:\n"
        f"{protected}\n\n"
        "Requirements:\n"
        "- minimal reversible change\n"
        "- no look-ahead\n"
        "- no risk-engine, live-execution, secret, or kill-switch changes\n"
        "- tests are mandatory\n"
        "- explain the change\n"
        "- a new dataset_version is required if the sample changes\n"
        "- do not retune against an out-of-sample set that was already used\n"
    )
