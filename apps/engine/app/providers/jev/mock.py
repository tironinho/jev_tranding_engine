from __future__ import annotations

import time

from app.providers.jev.schemas import JevAssessment, JevMarketRequest


def _unit(value: float) -> float:
    return max(0.0, min(1.0, 0.5 + 0.5 * value))


class MockJevProvider:
    """Deterministic plumbing double.

    The probabilities are a transparent rescaling of features already used by the baseline.
    They are not a market model and must not be read as evidence that Jev adds edge.
    """

    provider_name = "mock"
    provider_version = "mock-deterministic-v1"

    def __init__(self, prompt_version: str) -> None:
        self.prompt_version = prompt_version

    async def evaluate_market_state(self, request: JevMarketRequest) -> JevAssessment:
        started = time.perf_counter()
        features = request.features
        alignment = float(features.get("ema_alignment") or 0.0)
        flow = float(features.get("orderflow_delta_ratio") or 0.0)
        direction = 1.0 if (request.baseline_action or "LONG") == "LONG" else -1.0
        continuation = _unit(alignment * direction)
        atr_norm = float(features.get("atr_normalized") or 0.0)
        assessment = JevAssessment(
            provider="mock",
            is_mock=True,
            provider_version=self.provider_version,
            prompt_version=self.prompt_version,
            model="mock",
            trend_continuation_probability=continuation,
            reversal_probability=1 - continuation,
            buying_pressure_probability=_unit(flow),
            selling_pressure_probability=_unit(-flow),
            false_breakout_probability=_unit(-abs(alignment)),
            volatility_expansion_probability=_unit(atr_norm / 0.01),
            liquidity_sweep_probability=_unit(-abs(flow) * 0.5),
            latency_ms=(time.perf_counter() - started) * 1000,
            error=None,
        )
        return assessment
