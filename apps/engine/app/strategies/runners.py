from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import BaselineWeightConfig, CombinationConfig, RiskLimits
from app.domain.enums import (
    BASELINE_NO_TRADE,
    BASELINE_CONTEXT_CANDIDATE,
    BAD_SPREAD,
    CLASS_DIVERGENT,
    CLASS_FLOW_AGAINST,
    CLASS_SINGLE_DRIVER,
    HIGH_VOLATILITY,
    INSUFFICIENT_EXPECTED_EDGE,
    INSUFFICIENT_HISTORY,
    JEV_FALLBACK,
    JEV_UNAVAILABLE,
    OPENAI_CALLS_DISABLED,
    OPENAI_CIRCUIT_OPEN,
    OPENAI_ERROR,
    OPENAI_INVALID_SCHEMA,
    OPENAI_NOT_CONFIGURED,
    OPENAI_TIMEOUT,
    STALE_MARKET_DATA,
    Action,
    OperatingMode,
    SignalStatus,
)
from app.domain.schemas import MarketSnapshot, StrategyDecision
from app.providers.jev.schemas import JevMarketRequest, JevNotImplemented, JevProviderError, JevShadowDeferred
from app.providers.openai.schemas import OpenAICallError, OpenAIInvalidSchema, OpenAINotConfigured
from app.strategies.baseline_score import BaselineResult, score_baseline
from app.strategies.rules import apply_jev_veto, apply_openai_veto, break_size_scale, meta_hit_probability
from app.risk.plans import candidate_plans, score_probability
from app.analytics.setup_review import classify_setup


@dataclass
class StrategyContext:
    correlation_id: UUID
    opportunity_id: UUID
    mode: OperatingMode
    now: datetime
    weights: BaselineWeightConfig
    combination: CombinationConfig
    call_model: bool
    jev: Any = None
    openai: Any = None
    failure_policy: str = "NO_TRADE"
    risk: RiskLimits | None = None
    round_trip_fee: float = 0.001
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    intelligence: dict | None = None
    jev_scheduler: Any = None
    evaluation_deadline: float | None = None


def _decision(
    *,
    context: StrategyContext,
    snapshot: MarketSnapshot,
    strategy: str,
    action: Action,
    confidence: float,
    reasons: list[str],
    metadata: dict,
    model_version: str,
    prompt_version: str | None,
    started: float,
    status: SignalStatus = SignalStatus.VALID,
) -> StrategyDecision:
    if strategy == "baseline_jev":
        metadata = {**metadata, "setup_research": classify_setup(
            {"metadata": metadata}, snapshot.model_dump(mode="json"))}
    return StrategyDecision(
        correlation_id=context.correlation_id,
        opportunity_id=context.opportunity_id,
        snapshot_id=snapshot.snapshot_id,
        strategy=strategy,
        symbol=snapshot.symbol,
        timestamp=context.now,
        action=action,
        confidence=confidence,
        reason_codes=reasons,
        metadata=metadata,
        model_version=model_version,
        prompt_version=prompt_version,
        decision_latency_ms=(time.perf_counter() - started) * 1000,
        signal_status=status,
        mode=context.mode,
    )


class BaselineStrategy:
    key = "baseline"

    async def evaluate(self, snapshot: MarketSnapshot, features: dict, context: StrategyContext) -> StrategyDecision:
        started = time.perf_counter()
        result = score_baseline(snapshot, context.weights)
        return _decision(
            context=context,
            snapshot=snapshot,
            strategy=self.key,
            action=result.action,
            confidence=result.confidence,
            reasons=result.reason_codes,
            metadata={
                **_class_metadata(result),
                "combination_rule_version": context.combination.baseline_version,
            },
            model_version=result.version,
            prompt_version=None,
            started=started,
        )


class BaselineJevStrategy:
    key = "baseline_jev"

    async def evaluate(self, snapshot: MarketSnapshot, features: dict, context: StrategyContext) -> StrategyDecision:
        started = time.perf_counter()
        result = score_baseline(snapshot, context.weights)
        metadata = {
            **_class_metadata(result),
            "baseline_action": result.action.value,
            "combination_rule_version": context.combination.jev_rule_version,
        }
        _stamp_binance_agent(metadata, context.intelligence)
        context_candidate = False
        live_context_shadow = False
        baseline_result = result
        if result.action is Action.NO_TRADE:
            candidate_action = (None if (context.combination.jev_request_profile != "plan_outcome_v3"
                                         and not context.combination.review_below_threshold)
                                else _context_candidate_action(result, context.combination))
            if candidate_action is None:
                metadata["jev_effect"] = "idle"
                return _decision(
                    context=context,
                    snapshot=snapshot,
                    strategy=self.key,
                    action=Action.NO_TRADE,
                    confidence=result.confidence,
                    reasons=[BASELINE_NO_TRADE, *result.reason_codes],
                    metadata=metadata,
                    model_version=result.version,
                    prompt_version=None,
                    started=started,
                )
            context_candidate = True
            live_context_shadow = (context.mode is OperatingMode.LIVE
                                   or context.combination.jev_request_profile != "plan_outcome_v3")
            metadata.update(
                baseline_action=Action.NO_TRADE.value,
                candidate_action=candidate_action.value,
                candidate_min_abs_score=context.combination.candidate_min_abs_score,
                jev_evaluation_mode="READ_ONLY_SHADOW" if live_context_shadow else "ACTIVE",
            )
            result = replace(result, action=candidate_action)
        request = _jev_request(context, snapshot, result)
        metadata["jev_trade_plan"] = request.trade_plan
        if (context.risk and context.risk.cost_aware_target_floor and
                context.combination.jev_request_profile == "live_first_ten_2026_10_09"
                and request.trade_plan is None):
            return _decision(context=context, snapshot=snapshot, strategy=self.key,
                action=Action.NO_TRADE, confidence=0, reasons=["TRADE_PLAN_UNAVAILABLE"],
                metadata={**metadata, "jev_effect": "idle"}, model_version=result.version,
                prompt_version=request.prompt_version, started=started)
        blocked = _quality_gate(context, snapshot, metadata, started, self.key)
        if blocked is not None:
            return _force_live_context_shadow(blocked, baseline_result) if live_context_shadow else blocked
        if (context.risk and context.risk.dynamic_rr_enabled
                and context.combination.jev_request_profile == "plan_outcome_v3"):
            decision = await _dynamic_decision(context, snapshot, result, metadata, started)
            return _force_live_context_shadow(decision, baseline_result) if live_context_shadow else decision
        try:
            assessment = await _evaluate_jev(context, request, metadata)
        except JevShadowDeferred as exc:
            metadata.update(jev_effect="deferred", jev_defer_reason=str(exc))
            decision = _decision(context=context, snapshot=snapshot, strategy=self.key,
                action=Action.NO_TRADE, confidence=baseline_result.confidence,
                reasons=[BASELINE_NO_TRADE, "JEV_SHADOW_DEFERRED", *baseline_result.reason_codes],
                metadata=metadata, model_version=baseline_result.version, prompt_version=None, started=started)
            return _force_live_context_shadow(decision, baseline_result)
        except (JevNotImplemented, JevProviderError, Exception) as exc:
            error_type = type(exc).__name__
            context.artifacts.append({"kind": "jev", "error": str(exc), "error_type": error_type,
                                      "request": request.model_dump(mode="json")})
            if context.failure_policy == "FALLBACK_TO_BASELINE" and not context_candidate:
                metadata["jev_effect"] = "fallback"
                return _decision(
                    context=context,
                    snapshot=snapshot,
                    strategy=self.key,
                    action=result.action,
                    confidence=result.confidence,
                    reasons=[JEV_FALLBACK, *result.reason_codes],
                    metadata=metadata,
                    model_version=result.version,
                    prompt_version=request.prompt_version,
                    started=started,
                )
            metadata["jev_effect"] = "unavailable"
            decision = _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=0,
                reasons=[JEV_UNAVAILABLE],
                metadata={**metadata, "error": str(exc), "error_type": error_type},
                model_version=result.version,
                prompt_version=request.prompt_version,
                started=started,
            )
            return _force_live_context_shadow(decision, baseline_result) if live_context_shadow else decision
        context.artifacts.append(
            {
                "kind": "jev",
                "request": request.model_dump(mode="json"),
                "response": assessment.model_dump(mode="json"),
                "latency_ms": assessment.latency_ms,
                "provider_version": assessment.provider_version,
                "is_mock": assessment.is_mock,
                "error": assessment.error,
            }
        )
        required = _continuation_floor(snapshot, context, result.action, request.trade_plan)
        metadata["jev_required_continuation"] = required
        broke = _class_has_break(result)
        action, confidence, vetoes = apply_jev_veto(
            result.action,
            result.confidence,
            assessment,
            context.combination,
            breakout=broke,
            min_continuation=required,
        )
        _stamp_size(metadata, assessment, required, broke, action)
        _remember_jev(metadata, assessment, "veto" if action is Action.NO_TRADE else "confirm")
        if request.trade_plan and context.risk and context.risk.cost_aware_target_floor:
            assessed_plan = dict(request.trade_plan)
            assessed_plan.update(score_probability(assessed_plan, assessment.trend_continuation_probability,
                                                   context.risk.probability_haircut))
            if action is not Action.NO_TRADE and assessed_plan["expected_net_r"] <= 0:
                action = Action.NO_TRADE
                vetoes = ["JEV_NON_POSITIVE_EXPECTANCY"]
                metadata["jev_effect"] = "veto"
                metadata.pop("size_scale", None)
            assessed_plan.update(eligible=action is not Action.NO_TRADE,
                                 reason=",".join(vetoes) if vetoes else "JEV_CONFIRM",
                                 required_probability=required)
            metadata.update(jev_trade_plan=assessed_plan, candidate_plans=[assessed_plan],
                            selected_plan_id=assessed_plan["plan_id"], probability_status="UNCALIBRATED")
        decision = _decision(
            context=context,
            snapshot=snapshot,
            strategy=self.key,
            action=action,
            confidence=confidence,
            reasons=[*([BASELINE_CONTEXT_CANDIDATE] if context_candidate else []), *result.reason_codes, *vetoes],
            metadata=metadata,
            model_version=assessment.provider_version,
            prompt_version=assessment.prompt_version,
            started=started,
        )
        return _force_live_context_shadow(decision, baseline_result) if live_context_shadow else decision


_CONTEXT_CANDIDATE_BLOCKERS = {
    STALE_MARKET_DATA,
    INSUFFICIENT_HISTORY,
    BAD_SPREAD,
    HIGH_VOLATILITY,
    CLASS_SINGLE_DRIVER,
    CLASS_FLOW_AGAINST,
}


def _context_candidate_action(result: BaselineResult, config: CombinationConfig) -> Action | None:
    """Let coherent borderline signals reach context/JEV without weakening the baseline."""
    if abs(result.composite) < config.candidate_min_abs_score:
        return None
    if _CONTEXT_CANDIDATE_BLOCKERS.intersection(result.reason_codes):
        return None
    if CLASS_DIVERGENT in result.reason_codes and (
        result.market_class.drivers < 3
        or result.market_class.agreement < config.candidate_min_agreement
    ):
        return None
    if result.composite > 0:
        return Action.LONG
    if result.composite < 0:
        return Action.SHORT
    return None


def _force_live_context_shadow(decision: StrategyDecision, baseline_result: BaselineResult) -> StrategyDecision:
    """Persist the JEV assessment without promoting a rejected live baseline."""
    metadata = dict(decision.metadata)
    effect = metadata.get("jev_effect")
    metadata["jev_shadow_action"] = decision.action.value
    if effect in {"confirm", "veto", "unavailable", "deferred"}:
        metadata["jev_effect"] = f"shadow_{effect}"
    reasons = list(dict.fromkeys([BASELINE_NO_TRADE, *decision.reason_codes]))
    return decision.model_copy(
        update={
            "action": Action.NO_TRADE,
            "confidence": baseline_result.confidence,
            "reason_codes": reasons,
            "metadata": metadata,
        }
    )


async def _dynamic_decision(context, snapshot, result, metadata, started):
    plans = candidate_plans(
        snapshot,
        result.action,
        context.risk,
        context.round_trip_fee / 2,
        context.intelligence,
        allow_structure_extension=True,
    )
    metadata.update(candidate_plans=plans, target_rule_version="target_3r_v1",
                    probability_status="UNCALIBRATED")
    metadata.pop("jev_trade_plan", None)
    eligible = [p for p in plans if p["eligible"]][-3:]
    failures: list[tuple[str, str]] = []
    deferred: list[str] = []

    async def assess(plan):
        request = _jev_request(context, snapshot, result).model_copy(update={"trade_plan": dict(plan)})
        try:
            assessment = await _evaluate_jev(context, request, metadata)
        except JevShadowDeferred as exc:
            plan.update(eligible=False, reason="JEV_SHADOW_DEFERRED")
            deferred.append(str(exc))
            return None
        except Exception as exc:
            plan.update(eligible=False, reason="JEV_UNAVAILABLE")
            failure = (type(exc).__name__, str(exc))
            failures.append(failure)
            context.artifacts.append({"kind": "jev", "plan_id": plan["plan_id"],
                "request": request.model_dump(mode="json"), "error": failure[1], "error_type": failure[0]})
            return None
        context.artifacts.append({"kind": "jev", "plan_id": plan["plan_id"],
            "request": request.model_dump(mode="json"), "response": assessment.model_dump(mode="json"),
            "latency_ms": assessment.latency_ms, "provider_version": assessment.provider_version,
            "is_mock": assessment.is_mock, "error": assessment.error})
        plan.update(score_probability(plan, assessment.trend_continuation_probability, context.risk.probability_haircut))
        configured = context.combination.min_short_continuation if result.action is Action.SHORT else context.combination.min_trend_continuation
        required = configured
        action, confidence, vetoes = apply_jev_veto(result.action, result.confidence, assessment,
            context.combination, breakout=_class_has_break(result), min_continuation=required)
        accepted = action is not Action.NO_TRADE
        reasons = list(vetoes) if action is Action.NO_TRADE else []
        if accepted and plan["expected_net_r"] < context.risk.min_expected_net_r:
            accepted = False
            reasons = [INSUFFICIENT_EXPECTED_EDGE]
        plan.update(eligible=accepted, reason="JEV_CONFIRM" if accepted else ",".join(reasons), required_probability=required)
        return plan, assessment, confidence, accepted, reasons

    evaluated = await asyncio.gather(*(assess(plan) for plan in eligible))
    assessed = [item for item in evaluated if item is not None]
    choices = [item for item in assessed if item[3]]
    if not choices:
        reasons = ["TARGET_PLAN_UNAVAILABLE"] if not eligible else [JEV_UNAVAILABLE]
        if deferred and not failures:
            reasons = ["JEV_SHADOW_DEFERRED"]
        model_version = result.version
        prompt_version = None
        if assessed:
            best_plan, best_assessment, _confidence, _accepted, vetoes = max(
                assessed,
                key=lambda item: item[0]["expected_net_r"],
            )
            reasons = list(vetoes)
            metadata.update(
                jev_trade_plan=dict(best_plan),
                best_assessed_plan_id=best_plan["plan_id"],
                jev_stress_probability=best_plan["stress_probability"],
                jev_required_continuation=best_plan["required_probability"],
                jev_veto_reasons=list(vetoes),
            )
            _remember_jev(metadata, best_assessment, "veto")
            model_version = best_assessment.provider_version
            prompt_version = best_assessment.prompt_version
        else:
            metadata["jev_effect"] = "not_called" if not eligible else "unavailable"
            if deferred and not failures:
                metadata.update(jev_effect="deferred", jev_defer_reason=deferred[0])
            if failures:
                metadata["error_type"], metadata["error"] = failures[0]
        return _decision(context=context, snapshot=snapshot, strategy="baseline_jev", action=Action.NO_TRADE,
            confidence=result.confidence, reasons=[*_candidate_reasons(metadata), *reasons, *result.reason_codes], metadata=metadata,
            model_version=model_version, prompt_version=prompt_version, started=started)
    plan, assessment, confidence, _accepted, _reasons = max(choices, key=lambda item: item[0]["expected_net_r"])
    metadata.update(jev_trade_plan=dict(plan), selected_plan_id=plan["plan_id"],
                    jev_stress_probability=plan["stress_probability"], jev_required_continuation=plan["required_probability"])
    _remember_jev(metadata, assessment, "confirm")
    return _decision(context=context, snapshot=snapshot, strategy="baseline_jev", action=result.action,
        confidence=confidence, reasons=[*_candidate_reasons(metadata), "JEV_CONFIRM", "DYNAMIC_PLAN_SELECTED", *result.reason_codes], metadata=metadata,
        model_version=assessment.provider_version, prompt_version=assessment.prompt_version, started=started)


def _candidate_reasons(metadata: dict) -> list[str]:
    return [BASELINE_CONTEXT_CANDIDATE] if metadata.get("candidate_action") else []


async def _evaluate_jev(context, request, metadata):
    started = time.perf_counter()
    try:
        if context.jev_scheduler is not None:
            return await context.jev_scheduler.evaluate(context.jev, request,
                shadow=metadata.get("jev_evaluation_mode") == "READ_ONLY_SHADOW",
                deadline=context.evaluation_deadline)
        return await asyncio.wait_for(context.jev.evaluate_market_state(request), timeout=8)
    finally:
        metadata["jev_elapsed_ms"] = (time.perf_counter() - started) * 1000


class BaselineOpenAIJevStrategy:
    key = "baseline_openai_jev"

    async def evaluate(self, snapshot: MarketSnapshot, features: dict, context: StrategyContext) -> StrategyDecision:
        started = time.perf_counter()
        result = score_baseline(snapshot, context.weights)
        metadata: dict[str, Any] = {
            **_class_metadata(result),
            "baseline_action": result.action.value,
            "combination_rule_version": context.combination.openai_rule_version,
        }
        if not context.call_model:
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=0,
                reasons=[OPENAI_CALLS_DISABLED],
                metadata=metadata,
                model_version=None,
                prompt_version=None,
                started=started,
                status=SignalStatus.SKIPPED,
            )
        abstained = result.action is Action.NO_TRADE
        context_payload = {
            "timeframe_context": "15m",
            "timeframe_setup": "5m",
            "timeframe_timing": "1m",
            "quantitative_regime": snapshot.quantitative_regime,
        }
        try:
            record = await context.openai.interpret(
                symbol=snapshot.symbol,
                timestamp=snapshot.timestamp.isoformat(),
                features=dict(snapshot.features),
                context=context_payload,
            )
        except OpenAINotConfigured:
            return _no_ai(context, snapshot, metadata, OPENAI_NOT_CONFIGURED, started, SignalStatus.SKIPPED)
        except OpenAIInvalidSchema as exc:
            metadata["error"] = str(exc)[:300]
            return _no_ai(context, snapshot, metadata, OPENAI_INVALID_SCHEMA, started, SignalStatus.VALID)
        except OpenAICallError as exc:
            code = OPENAI_CIRCUIT_OPEN if "CIRCUIT" in str(exc) else OPENAI_ERROR
            if "timeout" in str(exc).lower():
                code = OPENAI_TIMEOUT
            return _no_ai(context, snapshot, metadata, code, started, SignalStatus.VALID)
        except Exception as exc:
            metadata["error"] = str(exc)
            return _no_ai(context, snapshot, metadata, OPENAI_ERROR, started, SignalStatus.VALID)
        state = record.market_state
        context.artifacts.append(
            {
                "kind": "openai",
                "request_id": record.request_id,
                "model": record.model,
                "prompt_version": record.prompt_version,
                "prompt_sha256": record.prompt_sha256,
                "input": record.input_payload,
                "output": record.output_payload,
                "latency_ms": record.latency_ms,
                "token_usage": record.token_usage,
                "estimated_cost": record.estimated_cost,
                "error": record.error,
            }
        )
        metadata["openai_latency_ms"] = record.latency_ms
        if state is None:
            return _no_ai(context, snapshot, metadata, OPENAI_INVALID_SCHEMA, started, SignalStatus.VALID)
        metadata["openai_regime"] = state.market_regime
        metadata["openai_confidence"] = state.confidence
        metadata["openai_reversal_risk"] = state.reversal_risk
        blocks = apply_openai_veto(result.action, state, context.combination)
        if blocks and not abstained:
            metadata["openai_effect"] = "veto"
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=state.confidence,
                reasons=blocks,
                metadata=metadata,
                model_version=record.model,
                prompt_version=record.prompt_version,
                started=started,
            )
        if abstained:
            metadata["openai_effect"] = "assessed"
            metadata["jev_effect"] = "idle"
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=result.confidence,
                reasons=[BASELINE_NO_TRADE, *result.reason_codes, *blocks],
                metadata=metadata,
                model_version=record.model,
                prompt_version=record.prompt_version,
                started=started,
            )
        request = _jev_request(context, snapshot, result, market_state=state.model_dump())
        metadata["jev_trade_plan"] = request.trade_plan
        blocked = _quality_gate(context, snapshot, metadata, started, self.key)
        if blocked is not None:
            return blocked
        try:
            assessment = await context.jev.evaluate_market_state(request)
        except (JevNotImplemented, JevProviderError, Exception) as exc:
            error_type = type(exc).__name__
            context.artifacts.append({"kind": "jev", "error": str(exc), "error_type": error_type,
                                      "request": request.model_dump(mode="json")})
            if context.failure_policy == "FALLBACK_TO_BASELINE":
                metadata["jev_effect"] = "fallback"
                return _decision(
                    context=context,
                    snapshot=snapshot,
                    strategy=self.key,
                    action=result.action,
                    confidence=result.confidence,
                    reasons=[JEV_FALLBACK, *result.reason_codes],
                    metadata=metadata,
                    model_version=record.model,
                    prompt_version=record.prompt_version,
                    started=started,
                )
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=0,
                reasons=[JEV_UNAVAILABLE],
                metadata={**metadata, "error": str(exc), "error_type": error_type},
                model_version=record.model,
                prompt_version=record.prompt_version,
                started=started,
            )
        context.artifacts.append(
            {
                "kind": "jev",
                "request": request.model_dump(mode="json"),
                "response": assessment.model_dump(mode="json"),
                "latency_ms": assessment.latency_ms,
                "provider_version": assessment.provider_version,
                "is_mock": assessment.is_mock,
                "error": assessment.error,
            }
        )
        required = _continuation_floor(snapshot, context, result.action)
        metadata["jev_required_continuation"] = required
        broke = _class_has_break(result)
        action, confidence, vetoes = apply_jev_veto(
            result.action,
            result.confidence,
            assessment,
            context.combination,
            breakout=broke,
            min_continuation=required,
        )
        _stamp_size(metadata, assessment, required, broke, action)
        metadata["openai_effect"] = "confirm"
        _remember_jev(metadata, assessment, "veto" if action is Action.NO_TRADE else "confirm")
        return _decision(
            context=context,
            snapshot=snapshot,
            strategy=self.key,
            action=action,
            confidence=confidence,
            reasons=[*result.reason_codes, *vetoes],
            metadata=metadata,
            model_version=record.model,
            prompt_version=record.prompt_version,
            started=started,
        )


def _class_metadata(result: BaselineResult) -> dict[str, Any]:
    market_class = result.market_class
    return {
        "composite": result.composite,
        "scores": result.scores,
        "market_class": market_class.primary,
        "class_labels": list(market_class.labels),
        "class_agreement": market_class.agreement,
        "class_drivers": market_class.drivers,
    }


def _class_has_break(result: BaselineResult) -> bool:
    structural = {"BREAKOUT", "BREAKDOWN", "BULLISH_RECLAIM", "BEARISH_RECLAIM"}
    labels = result.reason_codes if result.version == "baseline_weights_v1" else result.market_class.labels
    return bool(structural.intersection(labels))


def _stamp_size(metadata: dict, assessment, required: float, broke: bool, action: Action) -> None:
    if action is Action.NO_TRADE:
        return
    metadata["size_scale"] = break_size_scale(assessment.trend_continuation_probability, required, broke)


def _stamp_binance_agent(metadata: dict, intelligence: dict | None) -> None:
    if not isinstance(intelligence, dict):
        return
    agent = intelligence.get("binance_agent")
    if not isinstance(agent, dict):
        return
    metadata["binance_agent_version"] = agent.get("version")
    metadata["binance_agent_state"] = agent.get("state")
    metadata["binance_agent_bias"] = agent.get("directional_bias")
    metadata["binance_agent_quality"] = agent.get("quality")
    metadata["binance_agent_risk_flags"] = list(agent.get("risk_flags") or [])


def _jev_request(
    context: StrategyContext,
    snapshot: MarketSnapshot,
    result: BaselineResult,
    market_state: dict | None = None,
) -> JevMarketRequest:
    from app.risk.economics import plan_geometry
    plan = None
    initial = context.combination.jev_request_profile != "plan_outcome_v3"
    cost_plan = (context.risk is not None and context.risk.cost_aware_target_floor
                 and context.combination.jev_request_profile == "live_first_ten_2026_10_09")
    if cost_plan:
        plans = candidate_plans(snapshot, result.action, context.risk,
                                context.round_trip_fee/2, context.intelligence)
        plan = plans[0] if plans else None
    elif context.risk is not None and not initial:
        entry = (snapshot.best_ask if result.action is Action.LONG else snapshot.best_bid) or snapshot.price
        geometry = plan_geometry(result.action, entry, snapshot.features, context.risk)
        if not isinstance(geometry, str):
            plan = {"entry": entry, "stop": geometry.stop, "target": geometry.target,
                    "horizon_minutes": context.risk.max_hold_minutes,
                    "round_trip_fee_rate": context.round_trip_fee}
    is_context_candidate = "BELOW_THRESHOLD" in result.reason_codes
    return JevMarketRequest(
        prompt_version=(("jev_" + context.combination.jev_request_profile + ("_compact_market_v2" if cost_plan else ""))
                        if initial else getattr(context.jev, "prompt_version", "jev_plan_outcome_v3")),
        evaluation_profile=context.combination.jev_request_profile,
        symbol=snapshot.symbol,
        market_type=snapshot.market_type.value,
        timestamp=snapshot.timestamp,
        features=dict(snapshot.features),
        baseline_action=result.action.value,
        baseline_confidence=result.confidence,
        baseline_scores=result.scores,
        baseline_class=result.market_class.primary,
        baseline_labels=list(result.market_class.labels),
        candidate_origin="BASELINE_BELOW_THRESHOLD" if is_context_candidate else None,
        baseline_threshold=context.weights.min_abs_score if is_context_candidate else None,
        candidate_threshold=context.combination.candidate_min_abs_score if is_context_candidate else None,
        market_state=market_state,
        intelligence=context.intelligence,
        trade_plan=plan,
    )


def _quality_gate(context, snapshot, metadata, started, strategy):
    intel = context.intelligence
    if not isinstance(intel, dict) or intel.get("gate") != "NO_TRADE":
        return None
    metadata["intelligence_gate"] = "NO_TRADE"
    metadata["jev_context_hash"] = intel.get("context_hash")
    return _decision(
        context=context,
        snapshot=snapshot,
        strategy=strategy,
        action=Action.NO_TRADE,
        confidence=0,
        reasons=["INTELLIGENCE_QUALITY"],
        metadata=metadata,
        model_version=None,
        prompt_version=None,
        started=started,
    )


def _continuation_floor(snapshot: MarketSnapshot, context: StrategyContext, action: Action, plan: dict | None = None) -> float:
    configured = context.combination.min_short_continuation if action is Action.SHORT else context.combination.min_trend_continuation
    if (context.risk is not None and context.risk.cost_aware_target_floor and plan
            and isinstance(plan.get("net_rr"), (int, float)) and plan["net_rr"] > 0):
        return max(configured, 1/(1+plan["net_rr"]))
    if context.combination.jev_request_profile != "plan_outcome_v3":
        if context.combination.jev_request_profile == "initial_live_2026_10_08":
            configured = context.combination.min_trend_continuation
        atr = snapshot.features.get("atr")
        if context.risk is not None and snapshot.price > 0 and isinstance(atr, (int, float)) and atr > 0:
            stop_pct = max(context.risk.min_stop_pct, context.risk.atr_stop_mult * atr / snapshot.price)
            return max(configured, meta_hit_probability(context.risk.rr_target_multiple, context.round_trip_fee, stop_pct))
    return configured


def _remember_jev(metadata: dict, assessment, effect: str) -> None:
    metadata["jev_effect"] = effect
    metadata["jev_is_mock"] = assessment.is_mock
    metadata["jev_continuation"] = assessment.trend_continuation_probability
    metadata["jev_reversal"] = assessment.reversal_probability
    metadata["jev_false_breakout"] = assessment.false_breakout_probability


def _no_ai(context, snapshot, metadata, reason, started, status):
    metadata = {**metadata, "openai_effect": "unavailable"}
    return _decision(
        context=context,
        snapshot=snapshot,
        strategy="baseline_openai_jev",
        action=Action.NO_TRADE,
        confidence=0,
        reasons=[reason],
        metadata=metadata,
        model_version=None,
        prompt_version=None,
        started=started,
        status=status,
    )
