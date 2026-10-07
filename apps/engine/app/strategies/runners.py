from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import BaselineWeightConfig, CombinationConfig
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
from app.strategies.baseline_score import score_baseline
from app.strategies.rules import apply_jev_veto, apply_openai_veto


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
    artifacts: list[dict[str, Any]] = field(default_factory=list)


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
                "composite": result.composite,
                "scores": result.scores,
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
            "composite": result.composite,
            "scores": result.scores,
            "baseline_action": result.action.value,
            "combination_rule_version": context.combination.jev_rule_version,
        }
        if result.action is Action.NO_TRADE:
            return _decision(
                context=context,
                snapshot=snapshot,
                strategy=self.key,
                action=Action.NO_TRADE,
                confidence=result.confidence,
                reasons=[BASELINE_NO_TRADE, *result.reason_codes],
                metadata=metadata,
                model_version=result.version,
                prompt_version=context.combination.jev_rule_version,
                started=started,
            )
        request = JevMarketRequest(
            prompt_version=getattr(context.jev, "prompt_version", "jev_market_v1"),
            symbol=snapshot.symbol,
            market_type=snapshot.market_type.value,
            timestamp=snapshot.timestamp,
            features=dict(snapshot.features),
            baseline_action=result.action.value,
            baseline_confidence=result.confidence,
            baseline_scores=result.scores,
        )
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
        action, confidence, vetoes = apply_jev_veto(
            result.action,
            result.confidence,
            assessment,
            context.combination,
            breakout=bool(snapshot.features.get("breakout") or snapshot.features.get("breakdown")),
        )
        metadata["jev_effect"] = "veto" if action is Action.NO_TRADE else "confirm"
        metadata["jev_is_mock"] = assessment.is_mock
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
            "composite": result.composite,
            "scores": result.scores,
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
        if result.action is Action.NO_TRADE:
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
        except OpenAIInvalidSchema:
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
        blocks = apply_openai_veto(result.action, state, context.combination)
        if blocks:
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
        request = JevMarketRequest(
            prompt_version=getattr(context.jev, "prompt_version", "jev_market_v1"),
            symbol=snapshot.symbol,
            market_type=snapshot.market_type.value,
            timestamp=snapshot.timestamp,
            features=dict(snapshot.features),
            baseline_action=result.action.value,
            baseline_confidence=result.confidence,
            baseline_scores=result.scores,
            market_state=state.model_dump(),
        )
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
        action, confidence, vetoes = apply_jev_veto(
            result.action,
            result.confidence,
            assessment,
            context.combination,
            breakout=bool(snapshot.features.get("breakout") or snapshot.features.get("breakdown")),
        )
        metadata["openai_effect"] = "confirm"
        metadata["jev_effect"] = "veto" if action is Action.NO_TRADE else "confirm"
        metadata["jev_is_mock"] = assessment.is_mock
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
