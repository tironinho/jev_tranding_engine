from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.execution.reconciliation import reconcile
from app.providers.binance_account import _margin
from app.execution.binance_live import BinanceExecutionProvider
from app.domain.enums import Action, OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from app.service import _consecutive_live_losses
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


def test_live_loss_streak_uses_only_trailing_losses_from_current_utc_day():
    now = clock()
    trades = [
        SimpleNamespace(mode="live", closed_at=now - timedelta(days=1), net_pnl=-1),
        SimpleNamespace(mode="paper", closed_at=now - timedelta(minutes=4), net_pnl=-1),
        SimpleNamespace(mode="live", closed_at=now - timedelta(minutes=3), net_pnl=1),
        SimpleNamespace(mode="live", closed_at=now - timedelta(minutes=2), net_pnl=-1),
        SimpleNamespace(mode="live", closed_at=now - timedelta(minutes=1), net_pnl=-2),
    ]

    assert _consecutive_live_losses(trades, now) == 2


def test_status_exposes_live_loss_circuit_and_intelligence_health():
    eng = engine()
    eng.risk.update_limits(max_consecutive_live_losses=3)
    now = datetime.now(timezone.utc)
    eng.accounts.accounts["baseline_jev"].trades.extend([
        SimpleNamespace(mode="live", closed_at=now - timedelta(minutes=2), net_pnl=-1),
        SimpleNamespace(mode="live", closed_at=now - timedelta(minutes=1), net_pnl=-2),
    ])
    eng.intelligence.cache.health.update({"coinalyze": "ONLINE", "cryptoquant": "DEGRADED", "coinmetrics": "ONLINE"})

    status = eng.status()

    assert status["risk"] == {
        "live_loss_streak": 2,
        "live_loss_manual_reset_at": None,
        "max_consecutive_live_losses": 3,
        "live_loss_circuit_open": False,
        "live_loss_reset_at": None,
    }
    assert status["intelligence"]["providers"]["cryptoquant"] == "DEGRADED"
    assert status["intelligence"]["degraded_providers"] == ["cryptoquant"]
    assert not status["intelligence"]["healthy"]


def test_daily_pause_reports_reset_and_does_not_expire_after_idle_hours(monkeypatch):
    eng = engine()
    eng.risk.update_limits(max_consecutive_live_losses=3)
    account = eng.accounts.accounts["baseline_jev"]
    account.trades.extend(SimpleNamespace(mode="live", net_pnl=-.01,
        closed_at=datetime(2026, 10, 14, 3, minute, tzinfo=timezone.utc)) for minute in (31, 34, 36))
    for hour in (6, 14, 23):
        moment = datetime(2026, 10, 14, hour, 59, tzinfo=timezone.utc)
        monkeypatch.setattr("app.service.utcnow", lambda: moment)
        risk = eng.status()["risk"]
        assert risk["live_loss_streak"] == 3 and risk["live_loss_circuit_open"]
        assert risk["live_loss_reset_at"] == "2026-10-15T00:00:00+00:00"
    moment = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)
    risk = eng.status()["risk"]
    assert risk["live_loss_streak"] == 0 and not risk["live_loss_circuit_open"]
    assert risk["live_loss_reset_at"] is None
    assert len(account.trades) == 3


@pytest.mark.parametrize("limit", [3, 10])
def test_manual_loss_reset_preserves_history_and_reblocks_at_configured_limit(monkeypatch, limit):
    cutoff = clock()
    eng = engine(live_loss_streak_reset_at=cutoff)
    eng.risk.update_limits(max_consecutive_live_losses=limit)
    account = eng.accounts.accounts["baseline_jev"]
    original = [SimpleNamespace(mode="live", closed_at=cutoff - timedelta(minutes=i), net_pnl=-.01)
        for i in (2, 1, 0)]
    account.trades.extend(original)
    moment = cutoff + timedelta(minutes=1)
    monkeypatch.setattr("app.service.utcnow", lambda: moment)
    assert eng.status()["risk"]["live_loss_streak"] == 0
    assert eng.status()["risk"]["live_loss_manual_reset_at"] == cutoff.isoformat()
    assert not eng.status()["risk"]["live_loss_circuit_open"]
    assert account.trades == original and sum(t.net_pnl for t in original) == -.03

    snapshot, _ = long_snapshot()
    snapshot = snapshot.model_copy(update={"timestamp": moment})
    assert eng._risk_context("baseline_jev", snapshot, live=True).consecutive_live_losses == 0
    # The operator reset is scoped to baseline_jev and leaves the paper book alone.
    eng.accounts.accounts["baseline"].trades.extend(original)
    assert eng._risk_context("baseline", snapshot, live=True).consecutive_live_losses == 3
    assert eng._risk_context("baseline_jev", snapshot, live=False).consecutive_live_losses == 0

    for count in range(1, limit + 1):
        moment = cutoff + timedelta(minutes=count)
        account.trades.append(SimpleNamespace(mode="live", closed_at=moment, net_pnl=-.01))
        snapshot = snapshot.model_copy(update={"timestamp": moment})
        risk = eng.status()["risk"]
        assert risk["live_loss_streak"] == count
        assert risk["live_loss_circuit_open"] == (count == limit)
        assert eng._risk_context("baseline_jev", snapshot, live=True).consecutive_live_losses == count
    # Recreating the process with the same configured timestamp keeps new losses.
    restarted = engine(live_loss_streak_reset_at=cutoff)
    restarted.risk.update_limits(max_consecutive_live_losses=limit)
    restarted.accounts.accounts["baseline_jev"].trades.extend(account.trades)
    assert restarted.status()["risk"]["live_loss_circuit_open"]
    assert restarted.risk.limits.max_consecutive_live_losses == limit
    assert len(account.trades) == 3 + limit and all(t.net_pnl == -.01 for t in account.trades)


@pytest.mark.parametrize("value", ["2026-01-01T12:00:00", "2999-01-01T12:00:00+00:00"])
def test_manual_loss_reset_rejects_naive_or_future_timestamps(value):
    with pytest.raises(ValueError):
        settings(live_loss_streak_reset_at=value)


def test_manual_loss_reset_accepts_empty_and_normalizes_timezone():
    assert settings(live_loss_streak_reset_at=" ").live_loss_streak_reset_at is None
    assert settings(live_loss_streak_reset_at="2026-01-01T09:00:00-03:00").live_loss_streak_reset_at == clock()


@pytest.mark.asyncio
async def test_manual_loss_reset_records_fixed_cutoff_in_audit(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(live_loss_streak_reset_at=clock())
    eng.postgres = SimpleNamespace(healthy=True, save_audit=AsyncMock())
    await eng._record_live_loss_reset()
    payload = eng.postgres.save_audit.call_args.args[0]
    assert payload["action"] == "live_loss_streak_reset_loaded"
    assert payload["actor"] == "operator_env"
    assert payload["payload"]["reset_at"] == clock().isoformat()
    assert payload["payload"]["trade_history_preserved"]


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
    assert p.protection_status == "RECOVERY_PENDING"
    assert p.recovery_attempts == 1
    assert p.next_recovery_at is not None
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
    eng.live.fetch_verified = AsyncMock(side_effect=[
        {"status": "FILLED", "executedQty": "1", "avgPrice": "100.01"},
        {"status": "NOT_FOUND", "executedQty": "0"},
    ])
    await eng._recover_pending_entries()
    assert not eng.pending_entries
    assert eng.accounts.accounts["baseline"].sole("BTCUSDT").quantity == 1
    eng.live.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_reuses_an_exchange_stop_accepted_before_crash(monkeypatch):
    from tests.test_runtime import _arm_live, _Exchange
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(trading_live_enabled=True, allow_real_orders=True, binance_api_key="k", binance_api_secret="s")
    _arm_live(eng)
    snapshot, book = long_snapshot()
    attach_book(eng, book)
    eng.live.client = _Exchange()
    eng.live.submit = AsyncMock(side_effect=TimeoutError())
    await eng.evaluate_snapshot(snapshot)
    eng.live.fetch_verified = AsyncMock(side_effect=[
        {"status": "FILLED", "executedQty": "1", "avgPrice": "100.01"},
        {"status": "NEW", "executedQty": "0", "clientOrderId": "existing-stop"},
    ])
    eng.live.submit_stop = AsyncMock()

    await eng._recover_pending_entries()

    position = eng.accounts.accounts["baseline"].sole("BTCUSDT")
    assert position.protection_status == "PROTECTED"
    assert position.stop_client_order_id == "existing-stop"
    eng.live.submit.assert_awaited_once()
    eng.live.submit_stop.assert_not_awaited()


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
    assert position.protection_status == "RECOVERY_PENDING"
    assert position.recovery_attempts == 1
    eng.live.submit_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_protection_recovery_is_bounded_and_becomes_critical(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(binance_api_key="k", binance_api_secret="s")
    eng.risk.update_limits(max_protection_recovery_attempts=3)
    p = _open(eng, mode="live", stop_client_order_id=None)
    p.protection_status = "UNPROTECTED"
    eng.live.submit_close = AsyncMock(side_effect=TimeoutError())
    state = eng.states["BTCUSDT"]

    for _ in range(3):
        p.next_recovery_at = clock()
        await eng._exit_live("baseline", "BTCUSDT", p, "PROTECTION_FAILED", clock(), state)
    p.next_recovery_at = clock()
    await eng._exit_live("baseline", "BTCUSDT", p, "PROTECTION_FAILED", clock(), state)

    assert p.recovery_attempts == 3
    assert p.protection_status == "RECOVERY_EXHAUSTED"
    assert eng.live.submit_close.await_count == 3
    assert any(row["kind"] == "protection_critical" for row in eng.store.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("free_quote,expected_calls", [(20, 1), (4, 0)])
async def test_margin_short_below_notional_uses_only_available_quote(free_quote, expected_calls):
    eng = engine(market_type="margin")
    p = _open(eng, mode="live", side=Action.SHORT, quantity=3.6,
              entry_price=1.4007, stop=1.41, target=1.38, stop_client_order_id=None)
    state = eng.states["BTCUSDT"]
    state.best_ask = state.last_price = 1.37
    eng.feed.rules["BTCUSDT"] = {"step_size": 0.1, "min_notional": 5}
    eng.balance.snapshot = AsyncMock(return_value={"status": "ok", "assets": [
        {"asset": "BTC", "total": -3.436, "free": 0},
        {"asset": "USDT", "free": free_quote},
    ]})
    def filled(intent):
        return OrderRecord(client_order_id="close", decision_id=p.decision_id,
            strategy=p.strategy, symbol=p.symbol, side="BUY", order_type=OrderType.MARKET,
            status=OrderStatus.FILLED, mode="live", quantity=intent.quantity,
            filled_quantity=intent.quantity, average_fill_price=1.37, created_at=clock())
    eng.live.submit_close = AsyncMock(side_effect=filled)
    await eng._exit_live("baseline", p.symbol, p, "PROTECTION_FAILED", clock(), state)
    assert eng.live.submit_close.await_count == expected_calls
    if expected_calls:
        intent = eng.live.submit_close.call_args.args[0]
        assert intent.quantity == pytest.approx(3.7)
        assert intent.quantity * 1.37 >= 5.05
        assert not eng.accounts.accounts["baseline"].positions
        assert eng.accounts.accounts["baseline"].trades[0].quantity == 3.6
        assert eng.store.fills[-1]["quantity"] == pytest.approx(3.7)
        assert any(e["kind"] == "close_notional_adjustment" for e in eng.store.events)
    else:
        assert p.protection_error == "MIN_NOTIONAL_CLOSE_INSUFFICIENT_FREE_QUOTE"
        assert p.quantity == 3.6


@pytest.mark.asyncio
@pytest.mark.parametrize("error,started,expected", [
    ("Filter failure: NOTIONAL", False, 1),
    ("timeout", False, 0),
    ("Filter failure: NOTIONAL", True, 0),
])
async def test_exhausted_notional_recovery_restarts_only_once(error, started, expected):
    eng = engine(market_type="margin")
    p = _open(eng, mode="live", side=Action.SHORT, stop_client_order_id=None)
    p.protection_status = "RECOVERY_EXHAUSTED"
    p.protection_error = error
    p.recovery_attempts = 5
    p.notional_recovery_started = started
    state = eng.states[p.symbol]
    state.last_price = 100
    eng._exit_live = AsyncMock()
    await eng._manage_position("baseline", p.symbol, p, state, clock(), None)
    assert eng._exit_live.await_count == expected
    if expected:
        assert p.notional_recovery_started
        assert p.recovery_attempts == 0
        assert p.protection_status == "RECOVERY_PENDING"


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


@pytest.mark.asyncio
async def test_external_flat_reconciliation_requires_two_readings_and_no_debt(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(market_type="margin")
    p = _open(eng, mode="live", side=Action.SHORT, stop_client_order_id="old-stop",
              opened_at=clock()-timedelta(days=1))
    eng.live.fetch_verified = AsyncMock(side_effect=[{"status": "NEW"}, {"status": "CANCELED"}])
    eng.live.cancel = AsyncMock()
    eng.live.submit_close = AsyncMock()
    balance = {"status": "ok", "total_liability_btc": 0,
               "assets": [{"asset": "USDT", "total": 20}], "observed_at": clock().timestamp()-10}
    await eng._reconcile_external_flat(balance)
    assert eng.accounts.accounts["baseline"].positions
    await eng._reconcile_external_flat(balance)  # cached reading is not confirmation
    assert eng.accounts.accounts["baseline"].positions
    await eng._reconcile_external_flat({**balance, "observed_at": clock().timestamp()})
    assert not eng.accounts.accounts["baseline"].positions
    assert p.protection_status == "EXTERNAL_CLOSED_PENDING_AUDIT"
    assert p.closed_at is None  # exact execution time remains unknown
    assert not eng.accounts.accounts["baseline"].trades  # no invented PnL
    eng.live.cancel.assert_awaited_once()
    eng.live.submit_close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"total_liability_btc": .00001},
    {"assets": [{"asset": "XRP", "total": -3.4}]},
    {"status": "ERROR"},
    {"observed_at": 0},
])
async def test_external_reconciliation_never_archives_unverified_balance(change, monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(market_type="margin")
    p = _open(eng, mode="live", opened_at=clock()-timedelta(days=1))
    eng._flat_margin_observed = clock().timestamp()-10
    balance = {"status": "ok", "total_liability_btc": 0, "assets": [],
               "observed_at": clock().timestamp(), **change}
    await eng._reconcile_external_flat(balance)
    assert str(p.position_id) in eng.accounts.accounts["baseline"].positions


@pytest.mark.asyncio
async def test_external_reconciliation_checkpoint_failure_keeps_position_blocking(monkeypatch):
    monkeypatch.setattr("app.service.utcnow", clock)
    eng = engine(market_type="margin")
    p = _open(eng, mode="live", stop_client_order_id=None,
              opened_at=clock()-timedelta(days=1))
    p.protection_status = "RECOVERY_EXHAUSTED"
    cash_before = eng.accounts.accounts["baseline"].cash
    eng._flat_margin_observed = clock().timestamp()-10
    eng._checkpoint = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError):
        await eng._reconcile_external_flat({"status": "ok", "total_liability_btc": 0,
            "assets": [{"asset": "USDT", "total": 20}], "observed_at": clock().timestamp()})
    restored = eng.accounts.accounts["baseline"].positions[str(p.position_id)]
    assert restored.protection_status == "RECOVERY_EXHAUSTED"
    assert eng.accounts.accounts["baseline"].cash == cash_before
