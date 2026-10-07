import pytest

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
    assert set(by_strategy) == {"baseline", "baseline_jev", "baseline_openai_jev"}
    assert len({item.snapshot_id for item in decisions}) == 1
    assert len({item.opportunity_id for item in decisions}) == 1
    assert by_strategy["baseline"].action is Action.LONG
    assert by_strategy["baseline_openai_jev"].signal_status.value == "skipped"
    assert by_strategy["baseline"].metadata["scores"] == by_strategy["baseline_jev"].metadata["scores"]
    assert eng.accounts.accounts["baseline"].positions["BTCUSDT"].quantity > 0
    assert eng.accounts.accounts["baseline_jev"].positions == {}
    assert eng.accounts.accounts["baseline_openai_jev"].positions == {}
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
    eng.strategy_settings["baseline_jev"].enabled = False
    eng.strategy_settings["baseline_openai_jev"].enabled = False
    first = await eng.evaluate_snapshot(snapshot)
    baseline = next(item for item in first if item.strategy == "baseline")
    qty_before = eng.accounts.accounts["baseline"].positions["BTCUSDT"].quantity
    from app.execution.paper import PaperExecutionProvider

    intent = OrderIntent(
        decision_id=baseline.decision_id,
        risk_id=None,
        strategy="baseline",
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
    assert order.client_order_id == again.client_order_id == baseline.decision_id.hex
    assert order.status is OrderStatus.FILLED
    assert fills_again == fills
    assert eng.accounts.accounts["baseline"].positions["BTCUSDT"].quantity == qty_before


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
