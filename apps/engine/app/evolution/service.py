from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Callable

import yaml

from app.config import Settings, discover_config_dir
from app.domain.mathutil import utcnow
from app.domain.schemas import TradeRecord
from app.evolution.agent_dispatcher import CodingAgentNotConfigured, build_provider, build_task_prompt
from app.evolution.anomaly_detector import detect_anomalies
from app.evolution.experiment_manager import ExperimentManager
from app.evolution.experiment_memory import ExperimentMemory
from app.evolution.metrics_comparator import summarize
from app.evolution.observer import EvolutionObserver
from app.evolution.promotion_engine import next_status, promotion_decision
from app.evolution.researcher import EvolutionResearcher
from app.evolution.schemas import (
    DECISION_NO_ACTION,
    RUNNING_EXPERIMENT_STATUSES,
    Anomaly,
    ExperimentStatus,
    ResearchReport,
    StrategyFamily,
    StrategyVersion,
    VersionStatus,
)


def _load_policy() -> dict:
    path = discover_config_dir() / "evolution.yaml"
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


class EvolutionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = _load_policy()
        self.memory = ExperimentMemory()
        self.experiments = ExperimentManager(self.memory)
        self.observer = EvolutionObserver(settings.initial_paper_equity)
        self.researcher = EvolutionResearcher(
            model=settings.openai_research_model,
            enabled=bool(settings.auto_research and settings.openai_api_key and settings.openai_research_model),
        )
        self.agent = build_provider(settings.coding_agent_provider)
        self.versions: dict[str, StrategyVersion] = {}
        self.anomalies: list[Anomaly] = []
        self.reports: list[ResearchReport] = []
        self.promotions: list[dict] = []
        self.controls = {
            "auto_research": settings.auto_research,
            "auto_build": settings.auto_build,
            "auto_shadow_promotion": settings.auto_shadow_promotion,
            "auto_paper_promotion": settings.auto_paper_promotion,
        }
        self._trades: Callable[[], list[TradeRecord]] = lambda: []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_decision = DECISION_NO_ACTION
        self._bootstrap()

    def bind_trades(self, source: Callable[[], list[TradeRecord]]) -> None:
        self._trades = source

    def _bootstrap(self) -> None:
        versions = (self.policy.get("bootstrap_versions") or {})
        defaults = {"baseline": "B-001", "baseline_jev": "J-001", "baseline_openai_jev": "OJ-001"}
        now = utcnow()
        digest = hashlib.sha256(b"baseline_weights_v1|features_v1|market_interpreter_v1|jev_market_v1").hexdigest()
        for family in (StrategyFamily.BASELINE, StrategyFamily.BASELINE_JEV, StrategyFamily.BASELINE_OPENAI_JEV):
            code = versions.get(family.value, defaults[family.value])
            self.versions[code] = StrategyVersion(
                strategy_family=family,
                version=code,
                status=VersionStatus.CHAMPION,
                created_at=now,
                created_by="bootstrap",
                config_hash=digest,
                prompt_version=None if family is StrategyFamily.BASELINE else "jev_market_v1",
                git_branch="main",
            )

    async def start(self) -> None:
        if not self.settings.evolution_engine_enabled:
            return
        self._task = asyncio.create_task(self._daily_loop(), name="evolution_daily")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _daily_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=86_400)
                return
            except TimeoutError:
                if self.controls["auto_research"]:
                    await self.run_analysis(actor="scheduler")

    async def run_analysis(self, actor: str = "operator") -> dict:
        trades = self._trades()
        observation = self.observer.collect(trades)
        self.anomalies = detect_anomalies(
            trades,
            min_sample=self.settings.min_experiment_sample_size,
            starting_equity=self.settings.initial_paper_equity,
        )
        if not self.anomalies:
            report = await self.researcher.analyze(None, self.memory)
            self.reports.append(report)
            self.last_decision = report.decision
            return {"decision": report.decision, "reason": report.reason, "anomalies": 0, "actor": actor, "observation": observation}
        created = 0
        for anomaly in self.anomalies:
            report = await self.researcher.analyze(anomaly, self.memory)
            self.reports.append(report)
            if report.decision != DECISION_NO_ACTION and self.controls["auto_build"] and created < self.settings.max_daily_experiments:
                created += 1
        self.last_decision = DECISION_NO_ACTION if not any(item.decision != DECISION_NO_ACTION for item in self.reports[-len(self.anomalies):]) else "PROPOSED"
        return {
            "decision": self.last_decision,
            "anomalies": len(self.anomalies),
            "experiments_created": created,
            "actor": actor,
            "observation": {"trade_count": observation["trade_count"]},
        }

    def status(self) -> dict:
        running = [item for item in self.experiments.experiments.values() if item.status in RUNNING_EXPERIMENT_STATUSES]
        champions = [item for item in self.versions.values() if item.status is VersionStatus.CHAMPION]
        return {
            "enabled": self.settings.evolution_engine_enabled,
            "status": "online" if self.settings.evolution_engine_enabled else "offline",
            "controls": self.controls,
            "coding_agent": getattr(self.agent, "name", "unknown"),
            "champions": len(champions),
            "challengers": len([item for item in self.versions.values() if item.status not in {VersionStatus.CHAMPION, VersionStatus.ARCHIVED}]),
            "anomalies": len(self.anomalies),
            "experiments_running": len(running),
            "experiments_rejected": len([item for item in self.experiments.experiments.values() if item.status is ExperimentStatus.REJECTED]),
            "last_decision": self.last_decision,
            "min_sample_size": self.settings.min_experiment_sample_size,
            "live_promotion": "disabled",
        }

    def champion_rows(self) -> list[dict]:
        trades = self._trades()
        rows = []
        now = datetime.now(timezone.utc)
        for version in self.versions.values():
            if version.status is not VersionStatus.CHAMPION:
                continue
            family_trades = [trade for trade in trades if trade.strategy == version.strategy_family.value]
            summary = summarize(family_trades, self.settings.initial_paper_equity)
            sample_ok = summary["trades"] >= self.settings.min_experiment_sample_size
            rows.append(
                {
                    "family": version.strategy_family.value,
                    "version": version.version,
                    "status": version.status.value,
                    "age_days": (now - version.created_at).days,
                    "trades": summary["trades"],
                    "expectancy_r": summary["expectancy_r"] if sample_ok else None,
                    "profit_factor": summary["profit_factor"] if sample_ok else None,
                    "max_drawdown": summary["max_drawdown"] if sample_ok else None,
                    "net_pnl": summary["net_pnl"] if summary["trades"] else None,
                    "health": None if not sample_ok else _health(summary),
                    "config_hash": version.config_hash,
                }
            )
        return rows

    def challenger_rows(self) -> list[dict]:
        return [
            version.model_dump(mode="json")
            for version in self.versions.values()
            if version.status not in {VersionStatus.CHAMPION, VersionStatus.ARCHIVED}
        ]

    def experiment_rows(self) -> list[dict]:
        rows = [item.model_dump(mode="json") for item in self.experiments.experiments.values()]
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return rows

    def experiment_detail(self, public_id: str) -> dict | None:
        experiment = self.experiments.get(public_id)
        if experiment is None:
            return None
        payload = experiment.model_dump(mode="json")
        payload["prompt"] = build_task_prompt(experiment)
        return payload

    def tree(self) -> list[dict]:
        nodes = []
        for version in self.versions.values():
            nodes.append(
                {
                    "version": version.version,
                    "family": version.strategy_family.value,
                    "parent": version.parent_version,
                    "status": version.status.value,
                }
            )
        return nodes

    def update_controls(self, changes: dict, actor: str) -> dict:
        allowed = {"auto_research", "auto_build", "auto_shadow_promotion", "auto_paper_promotion"}
        unknown = set(changes) - allowed
        if unknown:
            raise KeyError(",".join(sorted(unknown)))
        self.controls.update(changes)
        self.promotions.append({"at": utcnow().isoformat(), "actor": actor, "action": "controls", "changes": changes})
        return self.controls

    def promote(self, public_id: str, target: str, actor: str, *, auto: bool = False) -> dict:
        experiment = self.experiments.get(public_id)
        if experiment is None:
            return {"accepted": False, "reason": "NOT_FOUND"}
        gates = experiment.result.get("gates") or {}
        comparison = experiment.result.get("comparison")
        accepted, reason = promotion_decision(
            target=target,
            gates=gates,
            comparison=comparison,
            policy=self.policy.get("promotion") or {},
            auto=auto,
            auto_shadow=self.controls["auto_shadow_promotion"],
            auto_paper=self.controls["auto_paper_promotion"],
        )
        event = {
            "at": utcnow().isoformat(),
            "actor": actor,
            "action": "promote" if accepted else "promote_rejected",
            "from_version": experiment.parent_version,
            "to_version": experiment.challenger_version,
            "target": target,
            "reason": reason,
            "why": experiment.hypothesis,
        }
        self.promotions.append(event)
        if not accepted:
            return {"accepted": False, "reason": reason}
        experiment.status = next_status(target)
        experiment.timeline.append({"at": event["at"], "event": f"promoted_{target}"})
        return {"accepted": True, "status": experiment.status.value}

    def reject(self, public_id: str, reason: str, actor: str) -> dict:
        experiment = self.experiments.reject(public_id, reason)
        if experiment is None:
            return {"accepted": False, "reason": "NOT_FOUND"}
        self.promotions.append(
            {
                "at": utcnow().isoformat(),
                "actor": actor,
                "action": "reject",
                "from_version": experiment.parent_version,
                "to_version": experiment.challenger_version,
                "reason": reason,
                "why": experiment.hypothesis,
            }
        )
        return {"accepted": True, "status": experiment.status.value, "reason": reason}

    def cancel(self, public_id: str) -> dict:
        experiment = self.experiments.cancel(public_id)
        if experiment is None:
            return {"accepted": False, "reason": "NOT_FOUND"}
        return {"accepted": True, "status": experiment.status.value, "reason": experiment.rejection_reason}

    async def dispatch_mock(self, public_id: str) -> dict:
        experiment = self.experiments.get(public_id)
        if experiment is None:
            return {"accepted": False, "reason": "NOT_FOUND"}
        if not self.controls["auto_build"]:
            return {"accepted": False, "reason": "AUTO_BUILD_DISABLED"}
        prompt = build_task_prompt(experiment)
        try:
            result = await self.agent.create_experiment_task(
                {"task_id": public_id, "prompt": prompt, "branch": experiment.git_branch, "protected_enforced": True}
            )
        except CodingAgentNotConfigured as exc:
            return {"accepted": False, "reason": str(exc)}
        return {"accepted": True, "task": result}


def _health(summary: dict) -> float | None:
    expectancy = summary.get("expectancy_r")
    profit_factor = summary.get("profit_factor")
    drawdown = summary.get("max_drawdown")
    if expectancy is None or profit_factor is None or drawdown is None:
        return None
    raw = 50 + expectancy * 40 + min(profit_factor, 2) * 10 - drawdown * 50
    return max(0.0, min(100.0, raw))
