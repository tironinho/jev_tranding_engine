import inspect
import json

import httpx
import pytest

from app.execution.binance_live import BinanceExecutionProvider, LiveExecutionBlocked, sellable_quantity
from app.execution.paper import OrderIntent
from app.domain.enums import OrderType
from app.providers.jev.real import RealJevProvider
from app.providers.jev.schemas import JevMarketRequest
from app.providers.openai.prompts import PROMPT_SHA256, get_prompt
from app.providers.openai.provider import OpenAIProvider
from app.providers.openai.schemas import MarketState, OpenAIInvalidSchema
from app.strategies.rules import apply_jev_veto
from app.config import CombinationConfig
from app.domain.enums import Action
from app.providers.jev.schemas import JevAssessment
from tests.conftest import clock, engine, settings


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
async def test_dashboard_cannot_select_live_while_disarmed():
    eng = engine()
    with pytest.raises(PermissionError, match="LIVE_LOCKED"):
        await eng.update_strategy("baseline", enabled=None, mode="live", call_model=None, actor="test")
    assert eng.strategy_settings["baseline"].mode.value == "shadow"


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
async def test_margin_order_borrows_on_entry_and_never_withdraws():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["effect"] = request.url.params["sideEffectType"]
        seen["isolated"] = request.url.params["isIsolated"]
        assert "withdraw" not in request.url.path
        assert request.url.params["type"] == "MARKET"
        return httpx.Response(200, json={"status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "1", "clientOrderId": "x"})

    transport = httpx.MockTransport(handler)
    cfg = settings(
        market_type="margin",
        trading_live_enabled=True,
        allow_real_orders=True,
        binance_api_key="k",
        binance_api_secret="s",
        binance_account_rest_url="https://api.binance.com",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        order = await BinanceExecutionProvider(cfg, client=client).submit(_intent())
    assert seen["path"] == "/sapi/v1/margin/order"
    assert seen["effect"] == "AUTO_BORROW_REPAY"
    assert seen["isolated"] == "FALSE"
    assert order.filled_quantity == 0.01


@pytest.mark.asyncio
async def test_oregon_relays_the_margin_order_to_singapore():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        body = json.loads(request.content)
        assert body["params"]["sideEffectType"] == "AUTO_BORROW_REPAY"
        assert "signature" not in body["params"]
        assert request.headers["authorization"] == "Bearer share"
        return httpx.Response(200, json={"status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "1"})

    transport = httpx.MockTransport(handler)
    cfg = settings(
        market_type="margin",
        trading_live_enabled=True,
        allow_real_orders=True,
        balance_upstream_url="https://gateway.example",
        balance_share_token="share",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        order = await BinanceExecutionProvider(cfg, client=client).submit(_intent())
    assert seen["host"] == "gateway.example"
    assert seen["path"] == "/api/binance/order"
    assert order.filled_quantity == 0.01


def test_sellable_quantity_steps_down_to_the_free_base():
    assert sellable_quantity(0.00029, 0.00028985, 0.00001) == pytest.approx(0.00028)
    assert sellable_quantity(0.00029, 0.00029, 0.00001) == pytest.approx(0.00029)
    assert sellable_quantity(0.00029, None, 0.00001) == pytest.approx(0.00029)


@pytest.mark.asyncio
async def test_a_buy_paid_in_base_holds_the_net_quantity():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "FILLED",
                "executedQty": "0.00029",
                "cummulativeQuoteQty": "23.6",
                "fills": [{"price": "81409", "qty": "0.00029", "commission": "0.00000015", "commissionAsset": "BTC"}],
            },
        )

    transport = httpx.MockTransport(handler)
    cfg = settings(
        market_type="margin",
        trading_live_enabled=True,
        allow_real_orders=True,
        binance_api_key="k",
        binance_api_secret="s",
        binance_account_rest_url="https://api.binance.com",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        order = await BinanceExecutionProvider(cfg, client=client).submit(_intent())
    assert order.filled_quantity == pytest.approx(0.00028985)


@pytest.mark.asyncio
async def test_a_rejected_close_keeps_the_exchange_reason_and_rotates_the_id():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["newClientOrderId"])
        if len(calls) == 1:
            return httpx.Response(400, text='{"code":-2010,"msg":"Account has insufficient balance for requested action."}')
        return httpx.Response(200, json={"status": "FILLED", "executedQty": "0.00028", "cummulativeQuoteQty": "23"})

    transport = httpx.MockTransport(handler)
    cfg = settings(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s", binance_account_rest_url="https://api.binance.com")
    intent = _intent()
    intent.side = "SELL"
    async with httpx.AsyncClient(transport=transport) as client:
        provider = BinanceExecutionProvider(cfg, client=client)
        with pytest.raises(LiveExecutionBlocked) as caught:
            await provider.submit_close(intent)
        order = await provider.submit_close(intent)
    assert "insufficient balance" in caught.value.reason
    assert calls[0] != calls[1]
    assert calls[0].endswith("C")
    assert order.filled_quantity == pytest.approx(0.00028)


@pytest.mark.asyncio
async def test_real_jev_calls_only_the_configured_url():
    source = inspect.getsource(RealJevProvider)
    assert "httpx" not in source
    assert "requests" not in source
    assert "aiohttp" not in source
    assert "https://" not in source
    request = JevMarketRequest(
        prompt_version="jev_market_v1",
        symbol="BTCUSDT",
        market_type="futures",
        timestamp=clock(),
        features={
            "ema_alignment": 1,
            "price": 50000,
            "range_60m": 500,
            "range_60m_frac": 0.01,
            "taker_flow_1m": -0.2,
            "return_60m": 0.004,
            "breakout": False,
            "breakdown": True,
        },
        baseline_action="SHORT",
        baseline_confidence=0.7,
        baseline_scores={"trend_score": -0.4},
        baseline_class="CLASS_ALIGNED",
        baseline_labels=["CLASS_ALIGNED", "BREAKDOWN"],
    )
    missing = RealJevProvider(prompt_version="jev_market_v1", base_url="", api_key="", model="m")
    with pytest.raises(Exception):
        await missing.evaluate_market_state(request)

    class _Response:
        status_code = 200

        def json(self):
            names = (
                "trend_continuation_probability",
                "reversal_probability",
                "false_breakout_probability",
            )
            return {"model": "jev-1.13.0", "answers": {name: {"type": "noul", "noul": 0.8 if name.startswith("trend") else 0.2} for name in names}}

    class _Client:
        def __init__(self):
            self.urls: list[str] = []
            self.body: dict | None = None

        async def post(self, url, headers=None, json=None, timeout=None):
            self.urls.append(url)
            self.body = json
            assert headers["Authorization"].startswith("Bearer ")
            assert json["model"] == "m"
            assert json["state"]["symbol"] == "BTCUSDT"
            assert json["questions"]["trend_continuation_probability"]["type"] == "noul"
            assert set(json["questions"]) == {
                "trend_continuation_probability",
                "reversal_probability",
                "false_breakout_probability",
            }
            assert "`baseline_action`" in json["questions"]["trend_continuation_probability"]["instructions"]
            assert "features" not in json["state"]
            assert "price" not in json["state"]
            assert json["state"]["baseline_class"] == "CLASS_ALIGNED"
            assert json["state"]["baseline_labels"] == ["CLASS_ALIGNED", "BREAKDOWN"]
            assert json["state"]["range_60m"] == 0.01
            assert json["state"]["taker_flow_1m"] == -0.2
            assert "symbol" not in json
            return _Response()

    client = _Client()
    provider = RealJevProvider(
        prompt_version="jev_market_v1",
        base_url="https://jev.example/evaluate",
        api_key="secret",
        model="m",
        client=client,
    )
    assessment = await provider.evaluate_market_state(request)
    assert client.urls == ["https://jev.example/evaluate"]
    assert assessment.provider == "real"
    assert assessment.is_mock is False
    assert assessment.trend_continuation_probability == 0.8


@pytest.mark.asyncio
async def test_thin_external_context_stays_off_the_jev_question():
    class _Response:
        status_code = 200

        def json(self):
            names = (
                "trend_continuation_probability",
                "reversal_probability",
                "false_breakout_probability",
            )
            return {"answers": {name: {"noul": 0.6} for name in names}}

    class _Client:
        def __init__(self):
            self.body: dict | None = None

        async def post(self, url, headers=None, json=None, timeout=None):
            self.body = json
            return _Response()

    def request(overall: float) -> JevMarketRequest:
        return JevMarketRequest(
            prompt_version="jev_market_v1",
            symbol="BTCUSDT",
            market_type="futures",
            timestamp=clock(),
            features={"range_60m_frac": 0.01},
            baseline_action="LONG",
            intelligence={"data_quality": {"overall": overall}, "microstructure": {"taker_imbalance_1m": 0.5}},
        )

    client = _Client()
    provider = RealJevProvider(
        prompt_version="jev_market_v1",
        base_url="https://jev.example/evaluate",
        api_key="secret",
        model="m",
        client=client,
    )
    await provider.evaluate_market_state(request(0.563))
    assert "intelligence" not in client.body["state"]
    await provider.evaluate_market_state(request(0.82))
    assert client.body["state"]["intelligence"]["data_quality"]["overall"] == 0.82


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
    unsure = assessment.model_copy(update={"trend_continuation_probability": 0.9, "reversal_probability": 0.2, "buying_pressure_probability": 0.8, "selling_pressure_probability": 0.1, "false_breakout_probability": 0.6})
    kept, _, kept_reasons = apply_jev_veto(Action.LONG, 0.8, unsure, CombinationConfig(), False)
    assert kept is Action.LONG
    assert "JEV_FALSE_BREAKOUT" not in kept_reasons
    vetoed, _, veto_reasons = apply_jev_veto(Action.LONG, 0.8, unsure.model_copy(update={"false_breakout_probability": 0.9}), CombinationConfig(), True)
    assert vetoed is Action.NO_TRADE
    assert "JEV_FALSE_BREAKOUT" in veto_reasons


def test_uncertain_continuation_does_not_confirm():
    assessment = JevAssessment(
        provider="mock",
        is_mock=True,
        provider_version="test",
        prompt_version="jev_market_v1",
        trend_continuation_probability=0.50,
        reversal_probability=0.40,
        false_breakout_probability=0.10,
        buying_pressure_probability=0.1,
        selling_pressure_probability=0.9,
    )
    blocked, _, reasons = apply_jev_veto(Action.LONG, 0.8, assessment, CombinationConfig(), False)
    assert blocked is Action.NO_TRADE
    assert "JEV_LOW_CONTINUATION" in reasons
    clear = assessment.model_copy(update={"trend_continuation_probability": 0.55, "reversal_probability": 0.64})
    kept, _, kept_reasons = apply_jev_veto(Action.LONG, 0.8, clear, CombinationConfig(), False)
    assert kept is Action.LONG
    assert "JEV_CONFIRM" in kept_reasons
    assert "JEV_PRESSURE_DISAGREES" not in kept_reasons
    assert "JEV_LIQUIDITY_SWEEP" not in kept_reasons
    short = assessment.model_copy(update={"trend_continuation_probability": 0.44, "reversal_probability": 0.40})
    confirmed, _, short_reasons = apply_jev_veto(Action.SHORT, 0.8, short, CombinationConfig(), False)
    assert confirmed is Action.SHORT
    assert "JEV_CONFIRM" in short_reasons
    still_long, _, long_reasons = apply_jev_veto(Action.LONG, 0.8, short, CombinationConfig(), False)
    assert still_long is Action.NO_TRADE
    assert "JEV_LOW_CONTINUATION" in long_reasons


def test_named_break_keeps_the_side_and_shrinks_size():
    from app.strategies.rules import break_size_scale

    soft = JevAssessment(
        provider="mock",
        is_mock=True,
        provider_version="test",
        prompt_version="jev_market_v1",
        trend_continuation_probability=0.28,
        reversal_probability=0.28,
        false_breakout_probability=0.61,
        buying_pressure_probability=0.5,
        selling_pressure_probability=0.5,
    )
    blocked, _, reasons = apply_jev_veto(Action.LONG, 0.64, soft, CombinationConfig(), False)
    assert blocked is Action.NO_TRADE
    assert "JEV_LOW_CONTINUATION" in reasons
    kept, _, kept_reasons = apply_jev_veto(Action.LONG, 0.64, soft, CombinationConfig(), True)
    assert kept is Action.LONG
    assert "JEV_CONFIRM" in kept_reasons
    assert break_size_scale(0.28, 0.55, True) == pytest.approx(0.28 / 0.55)
    assert break_size_scale(0.28, 0.55, False) == 1.0
    reversed_break, _, reversal_reasons = apply_jev_veto(
        Action.LONG,
        0.64,
        soft.model_copy(update={"reversal_probability": 0.70}),
        CombinationConfig(),
        True,
    )
    assert reversed_break is Action.NO_TRADE
    assert "JEV_HIGH_REVERSAL" in reversal_reasons
    false_break, _, false_reasons = apply_jev_veto(
        Action.LONG,
        0.64,
        soft.model_copy(update={"false_breakout_probability": 0.85}),
        CombinationConfig(),
        True,
    )
    assert false_break is Action.NO_TRADE
    assert "JEV_FALSE_BREAKOUT" in false_reasons


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
