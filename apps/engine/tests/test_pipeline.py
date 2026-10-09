import asyncio

import pytest

from datetime import timedelta

from app.domain.enums import Action, OperatingMode, OrderStatus, OrderType
from app.execution.paper import OrderIntent
from app.execution.slippage import SlippageConfig
from app.domain.enums import SlippageModelName
from tests.conftest import attach_book, clock, engine, long_snapshot


@pytest.mark.asyncio
async def test_same_snapshot_reaches_every_strategy_and_paper_is_isolated():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for key, cfg in eng.strategy_settings.items():
        cfg.mode = OperatingMode.PAPER if key == "baseline" else OperatingMode.SHADOW
        cfg.enabled = True
    decisions = await eng.evaluate_snapshot(snapshot)
    by_strategy = {item.strategy: item for item in decisions}
    assert set(by_strategy) == {"baseline", "baseline_jev"}
    assert len({item.snapshot_id for item in decisions}) == 1
    assert len({item.opportunity_id for item in decisions}) == 1
    assert by_strategy["baseline"].action is Action.LONG
    assert by_strategy["baseline"].metadata["scores"] == by_strategy["baseline_jev"].metadata["scores"]
    assert eng.accounts.accounts["baseline"].sole("BTCUSDT").quantity > 0
    assert eng.accounts.accounts["baseline_jev"].positions == {}
    assert len(eng.accounts.accounts["baseline"].trades) == 0


@pytest.mark.asyncio
async def test_one_strategy_crashing_does_not_stop_the_others():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)

    class Boom:
        key = "baseline_jev"

        async def evaluate(self, snapshot, features, context):
            raise RuntimeError("boom")

    eng.strategies["baseline_jev"] = Boom()
    decisions = await eng.evaluate_snapshot(snapshot)
    by_strategy = {item.strategy: item for item in decisions}
    assert by_strategy["baseline"].action is Action.LONG
    assert "STRATEGY_ERROR" in by_strategy["baseline_jev"].reason_codes
    assert eng.health["baseline_jev"]["errors"] == 1


@pytest.mark.asyncio
async def test_duplicate_client_order_does_not_open_two_positions():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    first = await eng.evaluate_snapshot(snapshot)
    jev = next(item for item in first if item.strategy == "baseline_jev")
    qty_before = eng.accounts.accounts["baseline_jev"].sole("BTCUSDT").quantity
    from app.execution.paper import PaperExecutionProvider

    intent = OrderIntent(
        decision_id=jev.decision_id,
        risk_id=None,
        strategy="baseline_jev",
        symbol="BTCUSDT",
        side="BUY",
        order_type=OrderType.MARKET,
        quantity=1,
        limit_price=None,
        mode="paper",
        created_at=clock(),
        best_bid=99.99,
        best_ask=100.01,
        book=book,
        fee_rate=0.0005,
    )
    order, fills = eng.paper.submit_market(intent, SlippageConfig(model=SlippageModelName.SPREAD_BASED))
    again, fills_again = eng.paper.submit_market(intent, SlippageConfig(model=SlippageModelName.SPREAD_BASED))
    assert order.client_order_id == again.client_order_id == jev.decision_id.hex
    assert order.status is OrderStatus.FILLED
    assert fills_again == fills
    assert eng.accounts.accounts["baseline_jev"].sole("BTCUSDT").quantity == qty_before


def test_limit_below_the_market_is_not_filled():
    eng = engine()
    snapshot, book = long_snapshot()
    from uuid import uuid4

    intent = OrderIntent(
        decision_id=uuid4(),
        risk_id=None,
        strategy="baseline",
        symbol="BTCUSDT",
        side="BUY",
        order_type=OrderType.LIMIT,
        quantity=1,
        limit_price=90,
        mode="paper",
        created_at=clock(),
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        book=book,
        fee_rate=0.0002,
    )
    order, fills = eng.paper.submit_limit(intent, clock())
    assert order.status is OrderStatus.MISSED
    assert fills == []


@pytest.mark.asyncio
async def test_each_paper_strategy_opens_its_own_book():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for cfg in eng.strategy_settings.values():
        cfg.mode = OperatingMode.PAPER
        cfg.enabled = True
    decisions = await eng.evaluate_snapshot(snapshot)
    by_strategy = {item.strategy: item for item in decisions}
    assert by_strategy["baseline"].action is Action.LONG
    assert by_strategy["baseline_jev"].action is Action.LONG
    assert eng.accounts.accounts["baseline"].sole("BTCUSDT").quantity > 0
    assert eng.accounts.accounts["baseline_jev"].sole("BTCUSDT").quantity > 0


@pytest.mark.asyncio
async def test_a_later_signal_does_not_add_to_the_same_symbol():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for key, cfg in eng.strategy_settings.items():
        cfg.mode = OperatingMode.PAPER if key == "baseline" else OperatingMode.SHADOW
        cfg.enabled = True
    await eng.evaluate_snapshot(snapshot)
    first = eng.accounts.accounts["baseline"].sole("BTCUSDT")
    later = snapshot.model_copy(update={"timestamp": snapshot.timestamp + timedelta(seconds=61)})
    await eng.evaluate_snapshot(later)
    opened = [item for item in eng.accounts.accounts["baseline"].positions.values() if item.symbol == "BTCUSDT"]
    assert len(opened) == 1
    assert opened[0].position_id == first.position_id
    assert any("EXISTING_POSITION" in risk["reject_reasons"] for risk in eng.store.risks.values())


@pytest.mark.asyncio
async def test_a_scrap_of_capital_does_not_open():
    eng = engine(max_symbol_exposure=0.00004)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for key, cfg in eng.strategy_settings.items():
        cfg.mode = OperatingMode.PAPER if key == "baseline" else OperatingMode.SHADOW
    await eng.evaluate_snapshot(snapshot)
    assert eng.accounts.accounts["baseline"].positions == {}
    assert any("ORDER_BELOW_MIN_NOTIONAL" in risk["reject_reasons"] for risk in eng.store.risks.values())


@pytest.mark.asyncio
async def test_an_expired_sibling_does_not_block_the_other_book():
    from app.domain.enums import SignalStatus
    from app.domain.schemas import StrategyDecision

    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)

    class Expired:
        key = "baseline"

        async def evaluate(self, snapshot, features, context):
            return StrategyDecision(
                correlation_id=context.correlation_id,
                opportunity_id=context.opportunity_id,
                snapshot_id=snapshot.snapshot_id,
                strategy=self.key,
                symbol=snapshot.symbol,
                timestamp=context.now,
                action=Action.NO_TRADE,
                confidence=0,
                reason_codes=["EXPIRED_SIGNAL"],
                metadata={},
                mode=context.mode,
                signal_status=SignalStatus.EXPIRED,
            )

    eng.strategies["baseline"] = Expired()
    await eng.evaluate_snapshot(snapshot)
    assert eng.accounts.accounts["baseline_jev"].sole("BTCUSDT").quantity > 0
    assert eng.accounts.accounts["baseline"].positions == {}
    assert all("VOTES_NOT_ARRIVED" not in risk["reject_reasons"] for risk in eng.store.risks.values())


@pytest.mark.asyncio
async def test_baseline_opens_a_new_symbol_while_another_entry_is_fresh():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for key, cfg in eng.strategy_settings.items():
        cfg.mode = OperatingMode.PAPER if key == "baseline" else OperatingMode.SHADOW
    account = eng.accounts.accounts["baseline"]
    account.last_entry_at = snapshot.timestamp - timedelta(seconds=5)
    account.last_entry_by_symbol["ETHUSDT"] = account.last_entry_at
    await eng.evaluate_snapshot(snapshot)
    assert account.sole("BTCUSDT").quantity > 0


@pytest.mark.asyncio
async def test_abstain_does_not_call_jev_and_does_not_open():
    eng = engine()
    snapshot, book = long_snapshot(
        ema_alignment=0,
        ema_20_slope=0,
        price_vs_ema20=0,
        rsi=50,
        roc=0,
        volume_ratio=1,
        orderflow_delta_ratio=0,
        imbalance_10=0,
        range_position=0.5,
        breakout=False,
    )
    attach_book(eng, book)
    decisions = await eng.evaluate_snapshot(snapshot)
    by_strategy = {item.strategy: item for item in decisions}
    jev = by_strategy["baseline_jev"]
    assert by_strategy["baseline"].action is Action.NO_TRADE
    assert jev.action is Action.NO_TRADE
    assert jev.metadata["jev_effect"] == "idle"
    assert jev.metadata["baseline_action"] == "NO_TRADE"
    assert "jev_continuation" not in jev.metadata
    assert eng.store.jev_calls == []
    assert eng.accounts.accounts["baseline"].positions == {}
    assert eng.accounts.accounts["baseline_jev"].positions == {}


@pytest.mark.asyncio
async def test_a_long_with_no_volume_score_still_asks_jev():
    eng = engine()
    snapshot, book = long_snapshot(volume_ratio=None, taker_flow_1m=None, orderflow_delta_ratio=None)
    attach_book(eng, book)
    for cfg in eng.strategy_settings.values():
        cfg.mode = OperatingMode.SHADOW
    decisions = await eng.evaluate_snapshot(snapshot)
    jev = next(item for item in decisions if item.strategy == "baseline_jev")
    assert "STRATEGY_ERROR" not in jev.reason_codes
    assert jev.metadata.get("jev_effect") in {"confirm", "veto"}
    assert eng.store.jev_calls


@pytest.mark.asyncio
async def test_two_symbols_ask_jev_at_the_same_time():
    eng = engine()
    # This test isolates symbol concurrency, with one plan per symbol.
    eng.risk.update_limits(dynamic_rr_enabled=False)
    first, book = long_snapshot()
    second = first.model_copy(update={"symbol": "ETHUSDT"})
    attach_book(eng, book)
    for cfg in eng.strategy_settings.values():
        cfg.mode = OperatingMode.SHADOW

    class _Overlap:
        def __init__(self, inner):
            self.inner = inner
            self.inflight = 0
            self.max_inflight = 0

        async def evaluate_market_state(self, request):
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                await asyncio.sleep(0.05)
                return await self.inner.evaluate_market_state(request)
            finally:
                self.inflight -= 1

    probe = _Overlap(eng.jev)
    eng.jev = probe
    await asyncio.gather(eng.evaluate_snapshot(first), eng.evaluate_snapshot(second))
    assert probe.max_inflight == 2


@pytest.mark.asyncio
async def test_candle_closes_in_one_burst_start_together():
    eng = engine()
    inflight = 0
    max_inflight = 0

    async def _fake(symbol, as_of, trigger):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        eng._evaluating.discard(symbol)

    eng.evaluate_symbol = _fake
    moment = clock()
    await eng.on_snapshot_trigger("BTCUSDT", moment, "kline_close_1m")
    await eng.on_snapshot_trigger("ETHUSDT", moment, "kline_close_1m")
    await eng._close_flush
    assert max_inflight == 2


@pytest.mark.asyncio
async def test_jev_review_shows_the_state_that_was_sent_and_the_answer():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    await eng.evaluate_snapshot(snapshot)
    rows = eng.jev_reviews(5)
    assert rows
    review = rows[0]
    assert review["symbol"] == "BTCUSDT"
    assert review["state"] is None  # mock is not an HTTP request
    assert review["request_recorded"] is False
    assert isinstance(review["response"]["trend_continuation_probability"], float)
    assert isinstance(review["response"]["reversal_probability"], float)
    assert isinstance(review["response"]["false_breakout_probability"], float)


@pytest.mark.asyncio
async def test_kill_switch_blocks_entries_and_resume_is_audited():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    await eng.kill_switch("tester")
    decisions = await eng.evaluate_snapshot(snapshot)
    assert all(item.action is Action.LONG or item.action is Action.NO_TRADE or item.signal_status.value == "skipped" for item in decisions)
    assert eng.accounts.accounts["baseline"].positions == {}
    risk = next(iter(eng.store.risks.values()))
    assert "ENGINE_DISABLED" in risk["reject_reasons"]
    await eng.resume("tester")
    assert eng.trading_enabled is True
    assert [row["action"] for row in eng.store.audit] == ["kill_switch", "resume"]


def _voted(snapshot, strategy: str, action: Action, continuation: float | None = None):
    from app.domain.enums import SignalStatus
    from app.domain.schemas import StrategyDecision

    metadata = {} if continuation is None else {"jev_continuation": continuation}
    return StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy=strategy,
        symbol=snapshot.symbol,
        timestamp=snapshot.timestamp,
        action=action,
        confidence=0.8,
        reason_codes=[],
        metadata=metadata,
        mode=OperatingMode.PAPER,
        signal_status=SignalStatus.VALID,
    )


def _fill_book(eng, snapshot, symbols: list[str]) -> None:
    from uuid import uuid4

    for symbol in symbols:
        eng.accounts.open_position(
            strategy="baseline",
            symbol=symbol,
            side=Action.LONG,
            quantity=0.01,
            entry_price=100,
            stop=99,
            target=103,
            opened_at=snapshot.timestamp - timedelta(minutes=5),
            decision_id=uuid4(),
            entry_fee=0.01,
            initial_net_risk=1,
            margin=1,
        )


@pytest.mark.asyncio
async def test_a_full_book_opens_one_extra_only_when_both_sides_agree():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for cfg in eng.strategy_settings.values():
        cfg.mode = OperatingMode.PAPER
    eng.risk.update_limits(max_open_positions=3)
    _fill_book(eng, snapshot, ["ETHUSDT", "SOLUSDT", "BNBUSDT"])
    account = eng.accounts.accounts["baseline"]
    assert len(account.positions) == eng.risk.limits.max_open_positions

    await eng._execute_decisions(
        [
            _voted(snapshot, "baseline", Action.LONG),
            _voted(snapshot, "baseline_jev", Action.LONG, 0.80),
        ],
        snapshot,
    )
    assert account.sole("BTCUSDT").quantity > 0
    assert len(account.positions) == eng.risk.limits.max_open_positions + 1

    quiet = snapshot.model_copy(update={"timestamp": snapshot.timestamp + timedelta(minutes=2)})
    await eng._execute_decisions(
        [
            _voted(quiet, "baseline", Action.LONG),
            _voted(quiet, "baseline_jev", Action.NO_TRADE, 0.90),
        ],
        quiet,
    )
    assert len([item for item in account.positions.values() if item.symbol == "BTCUSDT"]) == 1
    assert any("EXISTING_POSITION" in risk["reject_reasons"] for risk in eng.store.risks.values())


@pytest.mark.asyncio
async def test_a_full_book_stays_shut_when_continuation_is_only_the_confirm_floor():
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    for cfg in eng.strategy_settings.values():
        cfg.mode = OperatingMode.PAPER
    eng.risk.update_limits(max_open_positions=3)
    _fill_book(eng, snapshot, ["ETHUSDT", "SOLUSDT", "BNBUSDT"])
    account = eng.accounts.accounts["baseline"]
    await eng._execute_decisions(
        [
            _voted(snapshot, "baseline", Action.LONG),
            _voted(snapshot, "baseline_jev", Action.LONG, 0.55),
        ],
        snapshot,
    )
    assert all(item.symbol != "BTCUSDT" for item in account.positions.values())
    assert any("MAX_OPEN_POSITIONS" in risk["reject_reasons"] for risk in eng.store.risks.values())
