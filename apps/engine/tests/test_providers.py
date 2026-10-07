import inspect

import httpx
import pytest

from app.execution.binance_live import BinanceExecutionProvider, LiveExecutionBlocked
from app.execution.paper import OrderIntent
from app.domain.enums import OrderType
from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import JevMarketRequest, JevNotImplemented
from app.providers.openai.prompts import PROMPT_SHA256, get_prompt
from app.providers.openai.provider import OpenAIProvider
from app.providers.openai.schemas import MarketState, OpenAIInvalidSchema
from app.strategies.rules import apply_jev_veto
from app.config import CombinationConfig
from app.domain.enums import Action
from app.providers.jev.schemas import JevAssessment
from tests.conftest import clock, settings


class _BoomClient:
    async def post(self, *args, **kwargs):
        raise AssertionError("live HTTP was called")


def _intent():
    from uuid import uuid4

    return OrderIntent(
        decision_id=uuid4(),
        risk_id=None,
        strategy="baseline",
        symbol="BTCUSDT",
        side="BUY",
        order_type=OrderType.MARKET,
        quantity=0.01,
        limit_price=None,
        mode="live",
        created_at=clock(),
        best_bid=100,
        best_ask=100.1,
        book=None,
        fee_rate=0.0005,
    )


@pytest.mark.asyncio
async def test_live_flags_block_before_any_http():
    cfg = settings(trading_live_enabled=False, allow_real_orders=False, binance_api_key="k", binance_api_secret="s")
    provider = BinanceExecutionProvider(cfg, client=_BoomClient())  # type: ignore[arg-type]
    with pytest.raises(LiveExecutionBlocked):
        await provider.submit(_intent())
    armed = settings(trading_live_enabled=True, allow_real_orders=False, binance_api_key="k", binance_api_secret="s")
    provider = BinanceExecutionProvider(armed, client=_BoomClient())  # type: ignore[arg-type]
    with pytest.raises(LiveExecutionBlocked):
        await provider.submit(_intent())


@pytest.mark.asyncio
async def test_real_jev_does_not_call_network():
    source = inspect.getsource(RealJevProvider)
    assert "httpx" not in source
    assert "requests" not in source
    assert "aiohttp" not in source
    provider = RealJevProvider(prompt_version="jev_market_v1", base_url="https://example.invalid", api_key="x", model="m")
    request = JevMarketRequest(
        prompt_version="jev_market_v1",
        symbol="BTCUSDT",
        market_type="futures",
        timestamp=clock(),
        features={},
    )
    with pytest.raises(JevNotImplemented):
        await provider.evaluate_market_state(request)


def test_prompt_v1_refuses_orders_and_is_locked():
    text = get_prompt("market_interpreter_v1")
    assert "do not place orders" in text.lower()
    assert "leverage" in text.lower()
    assert PROMPT_SHA256["market_interpreter_v1"] == __import__("hashlib").sha256(text.encode()).hexdigest()


def test_openai_schema_rejects_free_text_and_sizing_fields():
    with pytest.raises(Exception):
        MarketState.model_validate({"note": "buy now"})
    payload = {
        "market_regime": "range",
        "trend_strength": 0.5,
        "momentum_quality": 0.5,
        "orderflow_state": "balanced",
        "liquidity_state": "balanced",
        "breakout_quality": 0.2,
        "overextension": 0.2,
        "reversal_risk": 0.2,
        "anomaly_score": 0.1,
        "confidence": 0.6,
        "evidence": ["ema_alignment is flat"],
        "quantity": 5,
    }
    state = MarketState.model_validate(payload)
    assert not hasattr(state, "quantity") or "quantity" not in state.model_dump()


def test_jev_veto_is_explicit():
    assessment = JevAssessment(
        provider="mock",
        is_mock=True,
        provider_version="test",
        prompt_version="jev_market_v1",
        trend_continuation_probability=0.2,
        reversal_probability=0.8,
        buying_pressure_probability=0.3,
        selling_pressure_probability=0.7,
        false_breakout_probability=0.1,
        volatility_expansion_probability=0.4,
        liquidity_sweep_probability=0.1,
    )
    action, _confidence, reasons = apply_jev_veto(Action.LONG, 0.8, assessment, CombinationConfig(), False)
    assert action is Action.NO_TRADE
    assert reasons


@pytest.mark.asyncio
async def test_openai_invalid_body_is_rejected(monkeypatch):
    cfg = settings(openai_api_key="test-key", openai_model="gpt-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp_test", "model": "gpt-test", "output": [{"content": [{"type": "output_text", "text": "not-json"}]}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAIProvider(cfg, client=client)
        with pytest.raises(OpenAIInvalidSchema):
            await provider.interpret(symbol="BTCUSDT", timestamp=clock().isoformat(), features={"ema_alignment": 1}, context={})
