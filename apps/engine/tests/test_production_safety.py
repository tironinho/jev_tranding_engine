from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.execution.reconciliation import reconcile
from app.providers.binance_account import _margin
from app.execution.binance_live import BinanceExecutionProvider
from app.domain.enums import Action, OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from tests.conftest import engine, settings, clock, long_snapshot, attach_book
from tests.test_runtime import _open
from tests.test_providers import _intent


def test_every_asset_and_interest_only_debt_is_visible():
    assets = [{"asset": f"COIN{i}", "free": "0.1", "netAsset": "0.1"} for i in range(12)]
    assets.append({"asset": "ETH", "interest": "0.000004", "netAsset": "-0.000004"})
    balance = _margin({"userAssets": assets}, "margin")
    assert len(balance["assets"]) == 13
    assert balance["assets"][-1]["interest"] == 0.000004


def test_reconciliation_separates_cash_residue_interest_and_untracked_trade():
    balance = {"status": "ok", "assets": [
        {"asset": "USDT", "total": 38}, {"asset": "BTC", "total": .00000945},
        {"asset": "BNB", "total": -.027004, "interest": .000004},
        {"asset": "SOL", "total": 1}]}
    positions = [{"symbol": "BNBUSDT", "quantity": .027, "side": "SHORT", "mode": "live"}]
    result = reconcile(balance, positions, {"BTCUSDT": 83000, "BNBUSDT": 740, "SOLUSDT": 110}, {})
    by_asset = {r["asset"]: r for r in result["rows"]}
    assert by_asset["USDT"]["status"] == "CASH"
    assert by_asset["BTC"]["status"] == "RESIDUAL"
    assert by_asset["BNB"]["status"] == "MATCHED"
    assert by_asset["SOL"]["status"] == "MISMATCH"
    assert result["blocks_entry"]
    assert reconcile({"status": "ERROR"}, [], {}, {})["blocks_entry"]


def test_missing_exchange_position_is_not_silently_removed():
    result = reconcile({"status": "ok", "assets": []},
                       [{"symbol": "BTCUSDT", "quantity": .01, "side": "LONG", "mode": "live"}],
                       {"BTCUSDT": 80000}, {})
    assert result["blocks_entry"]
    assert result["rows"][0]["tracked_quantity"] == .01


def test_live_residual_does_not_consume_a_position_slot_or_block_the_symbol():
    eng = engine()
    position = _open(eng, mode="live")
    position.protection_status = "RESIDUAL"
    snapshot, _ = long_snapshot()
    context = eng._risk_context("baseline", snapshot, live=True)
    assert context.open_positions == 0
    assert context.open_stop_risk == 0
    assert not context.has_position_on_symbol


def test_partial_exit_preserves_remaining_position_margin_and_fee():
    eng = engine()
    p = _open(eng)
    trade = eng.accounts.close_position(strategy="baseline", position_id=str(p.position_id),
        exit_price=105, closed_at=clock(), exit_fee=.1, slippage=0, funding=0,
        exit_reason="TARGET", quantitative_regime=None, market_regime=None, quantity=.5)
    account = eng.accounts.accounts["baseline"]
    assert trade.quantity == .5
    assert trade.fees == pytest.approx(.225)
    assert account.positions[str(p.position_id)].quantity == 1.5
    assert account.margin_locked[str(p.position_id)] == 150
    assert account.positions[str(p.position_id)].entry_fee == .375
    assert account.positions[str(p.position_id)].status.value == "OPEN"


@pytest.mark.asyncio
async def test_stop_already_filled_on_target_never_submits_second_close():
    eng = engine()
    p = _open(eng, mode="live", stop_client_order_id="stop")
    eng.live.fetch_verified = AsyncMock(return_value={"status": "FILLED", "executedQty": "2", "avgPrice": "90"})
    eng.live.submit_close = AsyncMock()
    await eng._exit_live("baseline", "BTCUSDT", p, "TARGET", clock(), eng.states["BTCUSDT"])
    eng.live.submit_close.assert_not_called()
    assert not eng.accounts.accounts["baseline"].positions
    assert eng.accounts.accounts["baseline"].trades[0].exit_reason == "STOP"


@pytest.mark.asyncio
async def test_unknown_stop_status_blocks_market_exit():
    eng = engine()
    p = _open(eng, mode="live", stop_client_order_id="stop")
    eng.live.fetch_verified = AsyncMock(side_effect=TimeoutError())
    eng.live.submit_close = AsyncMock()
    await eng._exit_live("baseline", "BTCUSDT", p, "TIME", clock(), eng.states["BTCUSDT"])
    eng.live.submit_close.assert_not_called()
    assert p.quantity == 2


@pytest.mark.asyncio
async def test_cancel_race_reconciles_fill_before_market_exit():
    eng = engine()
    p = _open(eng, mode="live", stop_client_order_id="stop")
    eng.live.fetch_verified = AsyncMock(side_effect=[{"status": "NEW"}, {"status": "FILLED", "executedQty": "2", "avgPrice": "90"}])
    eng.live.cancel = AsyncMock(side_effect=RuntimeError("unknown order"))
    eng.live.submit_close = AsyncMock()
    await eng._exit_live("baseline", "BTCUSDT", p, "TIME", clock(), eng.states["BTCUSDT"])
    eng.live.submit_close.assert_not_called()
    assert len(eng.accounts.accounts["baseline"].trades) == 1


@pytest.mark.asyncio
async def test_restart_recovers_existing_close_without_resending():
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(200, json={"status": "FILLED", "executedQty": ".01", "avgPrice": "100"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BinanceExecutionProvider(settings(binance_api_key="k", binance_api_secret="s"), client)
        order = await provider.submit_close(_intent())
        assert order.filled_quantity == .01
    assert posts == []


def test_signal_age_and_book_age_checked_after_decision(monkeypatch):
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    from types import SimpleNamespace
    decision = SimpleNamespace(strategy="baseline", action=Action.LONG)
    monkeypatch.setattr("app.service.utcnow", lambda: clock() + timedelta(seconds=20))
    assert not eng._live_signal_fresh(decision, snapshot)
    monkeypatch.setattr("app.service.utcnow", clock)
    assert eng._live_signal_fresh(decision, snapshot)
    eng.states["BTCUSDT"].last_book_at = clock() - timedelta(seconds=10)
    assert not eng._live_signal_fresh(decision, snapshot)


def test_live_jev_has_time_for_close_delivery_and_account_reconciliation(monkeypatch):
    eng = engine()
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    from types import SimpleNamespace
    decision = SimpleNamespace(strategy="baseline_jev", action=Action.LONG)
    eng.states["BTCUSDT"].last_book_at = clock() + timedelta(seconds=24)
    monkeypatch.setattr("app.service.utcnow", lambda: clock() + timedelta(seconds=24))
    assert eng._live_signal_fresh(decision, snapshot)
    monkeypatch.setattr("app.service.utcnow", lambda: clock() + timedelta(seconds=31))
    fresh, details = eng._live_signal_freshness(decision, snapshot)
    assert not fresh
    assert details["signal_age_ms"] == 31_000
    assert details["max_signal_age_ms"] == 30_000


@pytest.mark.asyncio
async def test_failed_protection_attempts_close_and_retains_pending_balance(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()
    eng.live.submit_stop = AsyncMock(side_effect=RuntimeError("rejected"))
    eng.live.fetch_verified = AsyncMock(return_value={"status": "NOT_FOUND"})
    eng.live.submit_close = AsyncMock(side_effect=TimeoutError())
    await eng.evaluate_snapshot(snapshot)
    p = eng.accounts.accounts["baseline"].sole("BTCUSDT")
    assert p.protection_status == "UNPROTECTED"
    assert not eng.pending_entries
    eng.live.submit_close.assert_awaited_once()
    await eng.evaluate_snapshot(snapshot)
    assert any("UNPROTECTED_POSITION" in r["reject_reasons"] for r in eng.store.risks.values())


@pytest.mark.asyncio
async def test_ambiguous_entry_is_recovered_without_second_buy(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()
    eng.live.submit = AsyncMock(side_effect=TimeoutError())
    await eng.evaluate_snapshot(snapshot)
    assert len(eng.pending_entries) == 1
    eng.live.fetch_verified = AsyncMock(return_value={"status": "FILLED", "executedQty": "1", "avgPrice": "100.01"})
    await eng._recover_pending_entries()
    assert not eng.pending_entries
    assert eng.accounts.accounts["baseline"].sole("BTCUSDT").quantity == 1
    eng.live.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_long_tracks_net_base_after_entry_commission(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(
        trading_live_enabled=True,
        allow_real_orders=True,
        binance_api_key="k",
        binance_api_secret="s",
        market_type="margin",
    )
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()
    eng.balance.snapshot = AsyncMock(side_effect=[
        {"status": "ok", "equity_usdt": 10_000, "wallet": 10_000, "margin_level": 999,
         "assets": [{"asset": "USDT", "total": 10_000}]},
        {"status": "ok", "assets": [{"asset": "BTC", "free": 0.999, "total": 0.999}]},
    ])

    def filled(intent):
        return OrderRecord(
            client_order_id=intent.decision_id.hex,
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side="BUY",
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            mode="live",
            quantity=1,
            filled_quantity=1,
            average_fill_price=100,
            created_at=clock(),
            commissions={"BTC": 0.001},
        )

    def resting_stop(intent, _price, **_kwargs):
        return OrderRecord(
            client_order_id=intent.decision_id.hex[:31] + "S",
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side="SELL",
            order_type=OrderType.STOP_LOSS_LIMIT,
            status=OrderStatus.NEW,
            mode="live",
            quantity=intent.quantity,
            created_at=clock(),
        )

    eng.live.submit = AsyncMock(side_effect=filled)
    eng.live.submit_stop = AsyncMock(side_effect=resting_stop)
    await eng.evaluate_snapshot(snapshot)
    position = eng.accounts.accounts["baseline"].sole("BTCUSDT")
    assert position.quantity == pytest.approx(0.999)
    assert eng.store.fills[0]["quantity"] == pytest.approx(1)


@pytest.mark.asyncio
async def test_non_resting_stop_is_not_marked_protected(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()

    def rejected_stop(intent, _price, **_kwargs):
        return OrderRecord(
            client_order_id=intent.decision_id.hex[:31] + "S",
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side="SELL",
            order_type=OrderType.STOP_MARKET,
            status=OrderStatus.REJECTED,
            mode="live",
            quantity=intent.quantity,
            created_at=clock(),
        )

    eng.live.submit_stop = AsyncMock(side_effect=rejected_stop)
    eng.live.fetch_verified = AsyncMock(return_value={"status": "REJECTED", "executedQty": "0"})
    eng.live.submit_close = AsyncMock(side_effect=TimeoutError())
    await eng.evaluate_snapshot(snapshot)
    position = eng.accounts.accounts["baseline"].sole("BTCUSDT")
    assert position.protection_status == "UNPROTECTED"
    eng.live.submit_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_recovery_repairs_checkpoint_before_resolution(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()
    await eng.evaluate_snapshot(snapshot)
    entry = next(row for row in eng.store.orders.values() if row["order_type"] == "MARKET")
    decision_id = entry["decision_id"]
    decision = next(row for row in eng.store.decisions if row["decision_id"] == decision_id)
    eng.pending_entries[decision_id] = {
        "decision": decision,
        "snapshot": eng.store.snapshots[decision["snapshot_id"]],
        "risk": eng.store.risks[decision_id],
        "created_at": clock().isoformat(),
    }
    eng._checkpoint = AsyncMock()
    eng._resolve_entry = AsyncMock()
    await eng._recover_pending_entries()
    eng._checkpoint.assert_awaited_once()
    args = eng._checkpoint.await_args.kwargs
    assert args["positions"]
    assert args["orders"]
    assert args["fills"]
    eng._resolve_entry.assert_awaited_once_with(decision_id)


@pytest.mark.asyncio
async def test_live_breakeven_replaces_exchange_stop_before_local_update():
    from tests.test_runtime import _Exchange
    eng = engine(binance_api_key="k", binance_api_secret="s")
    eng.live.client = _Exchange()
    p = _open(eng, mode="live", stop=99, target=110, stop_client_order_id="original")
    await eng._step_stop("baseline", "BTCUSDT", p, {"max_bid": 101, "min_ask": 101.1})
    assert p.stop > 100
    assert p.stop_client_order_id.endswith("S1")
    assert p.protection_status == "PROTECTED"
    orders = list(eng.store.orders.values())
    assert orders[-1]["side"] == "SELL"
    assert orders[-1]["price"] == p.stop


@pytest.mark.asyncio
async def test_failed_live_stop_replacement_keeps_old_local_stop_and_attempts_exit():
    from tests.test_runtime import _Exchange
    eng = engine(binance_api_key="k", binance_api_secret="s")
    eng.live.client = _Exchange()
    p = _open(eng, mode="live", stop=99, target=110, stop_client_order_id="original")
    eng.live.submit_stop = AsyncMock(side_effect=TimeoutError())
    eng._exit_live = AsyncMock()
    await eng._step_stop("baseline", "BTCUSDT", p, {"max_bid": 101, "min_ask": 101.1})
    assert p.stop == 99
    assert p.protection_status == "UNPROTECTED"
    eng._exit_live.assert_awaited_once()
