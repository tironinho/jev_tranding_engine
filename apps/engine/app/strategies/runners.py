from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import BaselineWeightConfig, CombinationConfig, RiskLimits
from app.domain.enums import (
    BASELINE_NO_TRADE,
    JEV_FALLBACK,
    JEV_UNAVAILABLE,
    OPENAI_CALLS_DISABLED,
    OPENAI_CIRCUIT_OPEN,
    OPENAI_ERROR,
    OPENAI_INVALID_SCHEMA,
    OPENAI_NOT_CONFIGURED,
    OPENAI_TIMEOUT,
    Action,
    OperatingMode,
    SignalStatus,
)
from app.domain.schemas import MarketSnapshot, StrategyDecision
from app.providers.jev.schemas import JevMarketRequest, JevNotImplemented, JevProviderError
from app.providers.openai.schemas import OpenAICallError, OpenAIInvalidSchema, OpenAINotConfigured
from app.strategies.baseline_score import BaselineResult, score_baseline
from app.strategies.rules import apply_jev_veto, apply_openai_veto, break_size_scale, meta_hit_probability


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
        if result.action is Action.NO_TRADE:
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
        request = _jev_request(context, snapshot, result)
        metadata["jev_trade_plan"] = request.trade_plan
        blocked = _quality_gate(context, snapshot, metadata, started, self.key)
        if blocked is not None:
            return blocked
        try:
            assessment = await context.jev.evaluate_market_state(request)
        except (JevNotImplemented, JevProviderError, Exception) as exc:
            context.artifacts.append({"kind": "jev", "error": str(exc), "request": request.model_dump(mode="json")})
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
                    model_version=result.version,
                    prompt_version=request.prompt_version,
                    started=started,
                )
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=0,
                reasons=[JEV_UNAVAILABLE],
                metadata={**metadata, "error": str(exc)},
                model_version=result.version,
                prompt_version=request.prompt_version,
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
        _remember_jev(metadata, assessment, "veto" if action is Action.NO_TRADE else "confirm")
        return _decision(
            context=context,
            snapshot=snapshot,
            strategy=self.key,
            action=action,
            confidence=confidence,
            reasons=[*result.reason_codes, *vetoes],
            metadata=metadata,
            model_version=assessment.provider_version,
            prompt_version=assessment.prompt_version,
            started=started,
        )


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
            context.artifacts.append({"kind": "jev", "error": str(exc), "request": request.model_dump(mode="json")})
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
                metadata={**metadata, "error": str(exc)},
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
    return "BREAKOUT" in result.market_class.labels or "BREAKDOWN" in result.market_class.labels


def _stamp_size(metadata: dict, assessment, required: float, broke: bool, action: Action) -> None:
    if action is Action.NO_TRADE:
        return
    metadata["size_scale"] = break_size_scale(assessment.trend_continuation_probability, required, broke)


def _jev_request(
    context: StrategyContext,
    snapshot: MarketSnapshot,
    result: BaselineResult,
    market_state: dict | None = None,
) -> JevMarketRequest:
    from app.risk.economics import plan_geometry
    plan = None
    if context.risk is not None:
        entry = (snapshot.best_ask if result.action is Action.LONG else snapshot.best_bid) or snapshot.price
        geometry = plan_geometry(result.action, entry, snapshot.features, context.risk)
        if not isinstance(geometry, str):
            plan = {"entry": entry, "stop": geometry.stop, "target": geometry.target,
                    "horizon_minutes": context.risk.max_hold_minutes,
                    "round_trip_fee_rate": context.round_trip_fee}
    return JevMarketRequest(
        prompt_version=getattr(context.jev, "prompt_version", "jev_market_v1"),
        symbol=snapshot.symbol,
        market_type=snapshot.market_type.value,
        timestamp=snapshot.timestamp,
        features=dict(snapshot.features),
        baseline_action=result.action.value,
        baseline_confidence=result.confidence,
        baseline_scores=result.scores,
        baseline_class=result.market_class.primary,
        baseline_labels=list(result.market_class.labels),
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


def _continuation_floor(snapshot: MarketSnapshot, context: StrategyContext, action: Action) -> float:
    configured = context.combination.min_short_continuation if action is Action.SHORT else context.combination.min_trend_continuation
    if context.risk is None or snapshot.price <= 0:
        return configured
    atr = snapshot.features.get("atr")
    if not isinstance(atr, (int, float)) or atr <= 0:
        return configured
    stop_pct = max(context.risk.min_stop_pct, context.risk.atr_stop_mult * float(atr) / snapshot.price)
    return max(configured, meta_hit_probability(context.risk.rr_target_multiple, context.round_trip_fee, stop_pct))


def _remember_jev(metadata: dict, assessment, effect: str) -> None:
    metadata["jev_effect"] = effect
    metadata["jev_is_mock"] = assessment.is_mock
    metadata["jev_continuation"] = assessment.trend_continuation_probability
    metadata["jev_reversal"] = assessment.reversal_probability


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
