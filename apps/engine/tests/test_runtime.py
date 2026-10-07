import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.enums import QUEUE_SATURATED, Action, OperatingMode, OrderStatus, OrderType, SignalStatus
from app.domain.schemas import StrategyDecision, TradeRecord
from app.execution.binance_live import BinanceExecutionProvider, LiveExecutionBlocked
from tests.conftest import attach_book, clock, engine, long_snapshot, settings


def _open(eng, **overrides):
    values = dict(
        strategy="baseline",
        symbol="BTCUSDT",
        side=Action.LONG,
        quantity=2,
        entry_price=100,
        stop=90,
        target=120,
        opened_at=clock(),
        decision_id=uuid4(),
        entry_fee=0.5,
        initial_net_risk=10,
        margin=200,
        exit_fee_rate=0.0004,
        quantitative_regime="bull_trend|normal_volatility",
        market_regime="breakout",
    )
    values.update(overrides)
    return eng.accounts.open_position(**values)


def _arm_live(eng):
    eng.persistence_mode = "postgres"
    eng.db_healthy = True
    eng.feed.rules["BTCUSDT"] = {"step_size": 0.001, "tick_size": 0.01}
    for key, cfg in eng.strategy_settings.items():
        cfg.enabled = key == "baseline"
    eng.strategy_settings["baseline"].mode = OperatingMode.LIVE


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))

    def json(self) -> dict:
        return self._payload


class _Exchange:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, params=None, headers=None):
        self.calls.append(("POST", params))
        kind = params["type"]
        if kind == "STOP_MARKET":
            assert params["closePosition"] == "true"
            assert "quantity" not in params
            return _Response({"status": "NEW", "executedQty": "0", "clientOrderId": params["newClientOrderId"]})
        if kind == "MARKET":
            price = "108" if params.get("reduceOnly") == "true" else "100.01"
            return _Response(
                {
                    "status": "FILLED",
                    "executedQty": params["quantity"],
                    "avgPrice": price,
                    "clientOrderId": params["newClientOrderId"],
                }
            )
        raise AssertionError(kind)

    async def delete(self, url, params=None, headers=None):
        self.calls.append(("DELETE", params))
        return _Response({"status": "CANCELED"})

    async def get(self, url, params=None, headers=None):
        self.calls.append(("GET", params))
        return _Response({"status": "NEW", "executedQty": "0"})


def _posts(exchange: _Exchange, kind: str, reduce_only: bool | None = None) -> list[dict]:
    rows = []
    for method, params in exchange.calls:
        if method != "POST" or params.get("type") != kind:
            continue
        is_reduce = params.get("reduceOnly") == "true"
        if reduce_only is None or is_reduce is reduce_only:
            rows.append(params)
    return rows


def test_account_snapshot_restores_cash_position_and_overrides():
    eng = engine()
    _open(eng)
    fresh = engine()
    fresh.apply_runtime(
        {
            "accounts": eng.accounts.export_state(),
            "strategies": [
                {
                    "strategy_key": "baseline",
                    "enabled": False,
                    "mode": "shadow",
                    "config": {"call_model": False, "max_signal_age_ms": 1500},
                }
            ],
            "risk": {"min_net_rr": 2.25, "max_open_positions": 2},
        }
    )
    restored = fresh.accounts.accounts["baseline"]
    assert restored.cash == eng.accounts.accounts["baseline"].cash
    assert restored.positions["BTCUSDT"].quantity == 2
    assert restored.positions["BTCUSDT"].stop == 90
    assert fresh.strategy_settings["baseline"].mode is OperatingMode.SHADOW
    assert fresh.strategy_settings["baseline"].enabled is False
    assert fresh.strategy_settings["baseline"].max_signal_age_ms == 1500
    assert fresh.risk.limits.min_net_rr == 2.25
    assert fresh.risk.limits.max_open_positions == 2


def test_disarmed_restart_does_not_restore_live_mode():
    fresh = engine()
    fresh.apply_runtime(
        {"strategies": [{"strategy_key": "baseline", "enabled": True, "mode": "live", "config": {}}]}
    )
    assert fresh.strategy_settings["baseline"].mode is not OperatingMode.LIVE


def test_orphan_trades_rebuild_performance_when_no_account_snapshot_exists():
    now = clock()
    trade = TradeRecord(
        strategy="baseline",
        symbol="BTCUSDT",
        side=Action.LONG,
        quantity=1,
        entry_price=100,
        exit_price=110,
        stop=95,
        target=110,
        opened_at=now,
        closed_at=now,
        gross_pnl=10,
        fees=1,
        slippage=0,
        funding=0,
        net_pnl=9,
        r_multiple=1,
        mfe=10,
        mae=1,
        exit_reason="TARGET",
        decision_id=uuid4(),
        quantitative_regime="bull_trend|normal_volatility",
    )
    fresh = engine()
    fresh.apply_runtime({"orphan_trades": {"baseline": [trade.model_dump(mode="json")]}})
    assert fresh.accounts.accounts["baseline"].cash == 10_009
    assert fresh.performance()["baseline"]["trades"] == 1
    assert fresh.store.trades[0]["quantitative_regime"] == "bull_trend|normal_volatility"


@pytest.mark.asyncio
async def test_exit_uses_event_time_stored_fee_and_regime():
    eng = engine()
    _open(eng, target=110)
    eng.fee_config = replace(eng.fee_config, taker_fee_rate=0.05)
    state = eng.states["BTCUSDT"]
    state.last_price = 110
    state.best_bid = 110
    state.best_ask = 110.02
    state.book = None
    closed_at = clock() + timedelta(minutes=5)
    await eng.manage_positions("BTCUSDT", as_of=closed_at)
    trade = eng.accounts.accounts["baseline"].trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.closed_at == closed_at
    assert trade.fees == pytest.approx(0.5 + 110 * 2 * 0.0004)
    assert trade.quantitative_regime == "bull_trend|normal_volatility"
    assert trade.market_regime == "breakout"
    assert "bull_trend|normal_volatility" in eng.performance()["baseline"]["by_regime"]


@pytest.mark.asyncio
async def test_price_events_do_not_time_exit_on_the_wall_clock():
    eng = engine()
    _open(eng)
    state = eng.states["BTCUSDT"]
    state.last_price = 100
    state.best_bid = 100
    state.best_ask = 100.1
    await eng.on_price("BTCUSDT", 100, clock() + timedelta(minutes=30))
    await asyncio.gather(*list(eng._background))
    assert "BTCUSDT" in eng.accounts.accounts["baseline"].positions
    exit_at = clock() + timedelta(minutes=60)
    await eng.on_price("BTCUSDT", 100, exit_at)
    await asyncio.gather(*list(eng._background))
    trade = eng.accounts.accounts["baseline"].trades[0]
    assert trade.exit_reason == "TIME"
    assert trade.closed_at == exit_at


@pytest.mark.asyncio
async def test_saturated_symbol_does_not_start_a_second_evaluation():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for key, cfg in eng.strategy_settings.items():
        cfg.enabled = key == "baseline"
    started = asyncio.Event()
    release = asyncio.Event()

    class Hold:
        async def evaluate(self, snapshot, features, context):
            started.set()
            await release.wait()
            return StrategyDecision(
                correlation_id=context.correlation_id,
                opportunity_id=context.opportunity_id,
                snapshot_id=snapshot.snapshot_id,
                strategy="baseline",
                symbol=snapshot.symbol,
                timestamp=context.now,
                action=Action.NO_TRADE,
                confidence=0,
                reason_codes=["HOLD"],
                metadata={},
                mode=context.mode,
                signal_status=SignalStatus.VALID,
            )

    eng.strategies["baseline"] = Hold()
    await eng.on_snapshot_trigger("BTCUSDT", clock(), "kline_close_1m")
    await asyncio.wait_for(started.wait(), timeout=2)
    await eng.on_snapshot_trigger("BTCUSDT", clock(), "kline_close_1m")
    assert any(event["message"] == QUEUE_SATURATED for event in eng.store.events)
    release.set()
    await asyncio.gather(*list(eng._background))


@pytest.mark.asyncio
async def test_live_entry_opens_a_position_places_a_stop_and_blocks_the_next_candle():
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    exchange = _Exchange()
    eng.live.client = exchange
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    await eng.evaluate_snapshot(snapshot)
    position = eng.accounts.accounts["baseline"].positions["BTCUSDT"]
    assert position.mode == "live"
    assert position.quantity > 0
    assert position.stop_client_order_id.endswith("S")
    assert len(_posts(exchange, "MARKET", reduce_only=False)) == 1
    assert len(_posts(exchange, "STOP_MARKET")) == 1
    await eng.evaluate_snapshot(snapshot)
    assert len(_posts(exchange, "MARKET", reduce_only=False)) == 1
    assert len(eng.accounts.accounts["baseline"].positions) == 1


@pytest.mark.asyncio
async def test_live_target_cancels_the_stop_and_flattens():
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    exchange = _Exchange()
    eng.live.client = exchange
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    await eng.evaluate_snapshot(snapshot)
    state = eng.states["BTCUSDT"]
    state.best_bid = 108
    state.best_ask = 108.02
    state.last_price = 108
    closed_at = clock() + timedelta(minutes=5)
    await eng.manage_positions("BTCUSDT", as_of=closed_at)
    assert eng.accounts.accounts["baseline"].positions == {}
    assert any(method == "DELETE" for method, _params in exchange.calls)
    assert len(_posts(exchange, "MARKET", reduce_only=True)) == 1
    trade = eng.accounts.accounts["baseline"].trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.closed_at == closed_at
    assert trade.quantitative_regime == "bull_trend|normal_volatility"


@pytest.mark.asyncio
async def test_reduce_only_close_does_not_require_the_arm_flags():
    exchange = _Exchange()
    cfg = settings(trading_live_enabled=False, allow_real_orders=False, binance_api_key="k", binance_api_secret="s")
    provider = BinanceExecutionProvider(cfg, client=exchange)  # type: ignore[arg-type]
    from tests.test_providers import _intent

    with pytest.raises(LiveExecutionBlocked):
        await provider.submit(_intent())
    order = await provider.submit_close(_intent())
    assert order.status is OrderStatus.FILLED
    assert order.order_type is OrderType.MARKET
    assert _posts(exchange, "MARKET", reduce_only=True)
