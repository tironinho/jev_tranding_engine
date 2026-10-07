from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class StrategyFamily(StrEnum):
    BASELINE = "baseline"
    BASELINE_JEV = "baseline_jev"
    BASELINE_OPENAI_JEV = "baseline_openai_jev"
    ENSEMBLE = "ensemble"
    ML = "ml"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    TESTING = "testing"
    BACKTEST_PASSED = "backtest_passed"
    OOS_PASSED = "oos_passed"
    WALK_FORWARD_PASSED = "walk_forward_passed"
    MONTE_CARLO_PASSED = "monte_carlo_passed"
    SHADOW = "shadow"
    PAPER = "paper"
    CANDIDATE = "candidate"
    CHAMPION = "champion"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class ExperimentStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED_FOR_BUILD = "approved_for_build"
    BUILDING = "building"
    TESTING = "testing"
    BACKTESTING = "backtesting"
    OOS = "oos"
    WALK_FORWARD = "walk_forward"
    MONTE_CARLO = "monte_carlo"
    SHADOW = "shadow"
    PAPER = "paper"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    FAILED = "failed"


ACTIVE_FAMILIES = (
    StrategyFamily.BASELINE,
    StrategyFamily.BASELINE_JEV,
    StrategyFamily.BASELINE_OPENAI_JEV,
)

RUNNING_EXPERIMENT_STATUSES = {
    ExperimentStatus.PROPOSED,
    ExperimentStatus.APPROVED_FOR_BUILD,
    ExperimentStatus.BUILDING,
    ExperimentStatus.TESTING,
    ExperimentStatus.BACKTESTING,
    ExperimentStatus.OOS,
    ExperimentStatus.WALK_FORWARD,
    ExperimentStatus.MONTE_CARLO,
    ExperimentStatus.SHADOW,
    ExperimentStatus.PAPER,
}

REJECTION_OVERFITTING = "OVERFITTING"
REJECTION_DRAWDOWN = "DRAWDOWN_INCREASE"
REJECTION_OOS = "OOS_FAILURE"
REJECTION_WALK_FORWARD = "WALK_FORWARD_FAILURE"
REJECTION_MONTE_CARLO = "MONTE_CARLO_FAILURE"
REJECTION_SAMPLE = "INSUFFICIENT_SAMPLE"
REJECTION_AI_COST = "HIGH_AI_COST"
REJECTION_EFFECT = "LOW_EFFECT_SIZE"
REJECTION_PROTECTED = "PROTECTED_CODE_CHANGED"
REJECTION_DUPLICATE = "DUPLICATE_EXPERIMENT"
REJECTION_GATES = "GATES_INCOMPLETE"
REJECTION_LIVE = "LIVE_PROMOTION_DISABLED"
REJECTION_TESTS = "TESTS_FAILED"
REJECTION_CANCELLED = "CANCELLED"
DECISION_NO_ACTION = "NO_ACTION"
DECISION_PROPOSE = "PROPOSE_EXPERIMENT"


class StrategyVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    strategy_family: StrategyFamily
    version: str
    parent_version: str | None = None
    status: VersionStatus
    created_at: datetime
    created_by: str
    git_commit: str | None = None
    git_branch: str | None = None
    pull_request_url: str | None = None
    config_hash: str
    prompt_version: str | None = None
    feature_set_version: str = "features_v1"
    experiment_id: str | None = None
    previous_champion: str | None = None


class Experiment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    public_id: str
    strategy_family: StrategyFamily
    parent_version: str
    challenger_version: str | None = None
    hypothesis: str
    problem_statement: str
    target_symbol: str | None = None
    target_timeframe: str | None = None
    target_regime: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    source_analysis_id: str | None = None
    git_branch: str | None = None
    git_commit: str | None = None
    pull_request: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    decision: str | None = None
    rejection_reason: str | None = None
    hypothesis_fingerprint: str
    dataset_version: str | None = None
    code_locked: bool = False
    prompt_version: str | None = None
    seed: int | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class Anomaly(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    code: str
    strategy_family: StrategyFamily
    symbol: str | None = None
    detected_at: datetime
    sample_size: int
    expectancy: float | None = None
    profit_factor: float | None = None
    anomaly_confidence: float
    evidence: dict[str, Any] = Field(default_factory=dict)
    window: str


class RootCause(BaseModel):
    code: str
    explanation: str
    evidence: list[str] = Field(default_factory=list)


class RecommendedExperiment(BaseModel):
    hypothesis: str
    target_component: str
    expected_effect: str
    allowed_changes: list[str] = Field(default_factory=list)


class ResearchReport(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime
    prompt_version: str
    decision: str
    root_cause_hypotheses: list[str] = Field(default_factory=list)
    root_causes: list[RootCause] = Field(default_factory=list)
    recommended_experiments: list[str] = Field(default_factory=list)
    recommended_experiment: RecommendedExperiment | None = None
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    anomaly_id: str | None = None
    reason: str | None = None
    model: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    estimated_cost: float | None = None
    error: str | None = None
