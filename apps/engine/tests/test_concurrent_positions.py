from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.api.router import RiskPatch
from app.domain.enums import Action, MarketType, OperatingMode, OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from app.risk.engine import FeeQuote
from app.service import TradingEngine
from tests.conftest import attach_book, clock, engine, long_snapshot, settings
from tests.test_risk_limits import _context, _order
from tests.test_runtime import _arm_live, _open


def test_production_allows_pyramiding_and_zero_disables_position_count_limit():
    eng = TradingEngine(settings())
    assert eng.risk.limits.allow_pyramiding
    assert eng.risk.limits.max_open_positions == 0
    assert RiskPatch(max_open_positions=0).max_open_positions == 0
    with pytest.raises(ValueError):
        RiskPatch(max_open_positions=-1)


@pytest.mark.parametrize("extra_slot", [False, True])
def test_unlimited_count_accepts_same_side_without_claiming_an_extra_slot(extra_slot):
    eng = engine()
    eng.risk.update_limits(max_open_positions=0, allow_pyramiding=True)
    snapshot, book = long_snapshot()
    risk = eng.risk.evaluate(_order(snapshot, Action.LONG), snapshot,
        _context(open_positions=100, has_position_on_symbol=True,
                 position_sides_on_symbol=(Action.LONG,)), FeeQuote(.001, .001, "test"), book,
        extra_slot=extra_slot)
    assert risk.accepted, risk.reject_reasons
    assert not risk.details["extra_open_slot"]


def test_unlimited_count_keeps_financial_risk_and_configured_count_caps():
    eng = engine()
    eng.risk.update_limits(max_open_positions=0, allow_pyramiding=True)
    snapshot, book = long_snapshot()
    decision = _order(snapshot, Action.LONG)
    risk = eng.risk.evaluate(decision, snapshot, _context(open_stop_risk=200),
                            FeeQuote(.001, .001, "test"), book)
    assert "MAX_PORTFOLIO_STOP_RISK" in risk.reject_reasons
    eng.risk.update_limits(max_open_positions=2)
    risk = eng.risk.evaluate(decision, snapshot, _context(open_positions=2),
                            FeeQuote(.001, .001, "test"), book)
    assert "MAX_OPEN_POSITIONS" in risk.reject_reasons


@pytest.mark.parametrize("market,sides,reason", [
    ("margin", (Action.SHORT,), "OPPOSITE_POSITION_NETTING"),
    ("futures", (Action.LONG,), "FUTURES_CLOSE_ALL_POSITION"),
])
def test_owned_live_positions_are_not_netted_or_given_a_close_all_stop(market, sides, reason):
    eng = engine()
    eng.risk.update_limits(max_open_positions=0, allow_pyramiding=True, live_longs_enabled=True)
    snapshot, book = long_snapshot()
    risk = eng.risk.evaluate(_order(snapshot, Action.LONG), snapshot,
        _context(live_requested=True, live_armed=True, has_position_on_symbol=True,
                 market_type=market, position_sides_on_symbol=sides),
        FeeQuote(.001, .001, "test"), book)
    assert reason in risk.reject_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [Action.LONG, Action.SHORT])
async def test_two_margin_entries_have_independent_stops_and_closing_one_preserves_the_other(monkeypatch, side):
    moment = clock()
    monkeypatch.setattr("app.service.utcnow", lambda: moment)
    eng = TradingEngine(settings(trading_live_enabled=True, allow_real_orders=True,
                                 market_type="margin"))
    _arm_live(eng)
    # Exercise independent order lifecycle with allocation below the symbol cap.
    eng.risk.update_limits(risk_per_trade=.0005)
    snapshot, book = long_snapshot()
    snapshot = snapshot.model_copy(update={"market_type": MarketType.MARGIN})
    attach_book(eng, book)

    async def balance(**_kwargs):
        qty = sum(p.quantity for p in eng.accounts.accounts["baseline"].positions.values())
        quote=10000 + (qty*100.01 if side is Action.SHORT else -qty*100.01)
        return {"status":"ok", "equity_usdt":10000, "wallet":10000,
                "total_net_asset_btc":10000/eng.states["BTCUSDT"].last_price,
                "margin_level":999, "available":10000,
                "assets":[{"asset":"USDT", "free":quote, "total":quote},
                          {"asset":"BTC", "free":qty if side is Action.LONG else 0,
                           "total":qty if side is Action.LONG else -qty}]}
    eng.balance.snapshot = AsyncMock(side_effect=balance)

    def filled(intent):
        return OrderRecord(client_order_id=intent.decision_id.hex, decision_id=intent.decision_id,
            risk_id=intent.risk_id, strategy=intent.strategy, symbol=intent.symbol,
            side=intent.side, order_type=OrderType.MARKET, status=OrderStatus.FILLED,
            mode="live", quantity=intent.quantity, filled_quantity=intent.quantity,
            average_fill_price=100.01, created_at=moment, exchange_at=moment)

    def stopped(intent, _price, **_kwargs):
        return OrderRecord(client_order_id=intent.decision_id.hex[:31]+"S",
            decision_id=intent.decision_id, risk_id=intent.risk_id, strategy=intent.strategy,
            symbol=intent.symbol, side="SELL" if side is Action.LONG else "BUY", order_type=OrderType.STOP_LOSS_LIMIT,
            status=OrderStatus.NEW, mode="live", quantity=intent.quantity,
            filled_quantity=0, created_at=moment)

    eng.live.submit = AsyncMock(side_effect=filled)
    eng.live.submit_stop = AsyncMock(side_effect=stopped)
    first_decision = _order(snapshot, side).model_copy(update={"mode":OperatingMode.LIVE})
    await eng._execute_live(first_decision, snapshot)
    moment += timedelta(seconds=61)
    eng.states["BTCUSDT"].last_book_at=moment
    later = snapshot.model_copy(update={"timestamp":moment})
    second_decision = _order(later, side).model_copy(update={"mode":OperatingMode.LIVE})
    await eng._execute_live(second_decision, later)

    positions=list(eng.accounts.accounts["baseline"].positions.values())
    assert len(positions)==2, [(r["accepted"], r["reject_reasons"], r.get("details"))
                              for r in eng.store.risks.values()]
    first, second=positions
    assert first.position_id != second.position_id
    assert first.stop_client_order_id != second.stop_client_order_id
    assert all(p.protection_status=="PROTECTED" for p in positions)
    restored=TradingEngine(settings(market_type="margin"))
    restored.apply_runtime({"accounts":eng.accounts.export_state(), "risk":{"max_open_positions":0}})
    assert restored.risk.limits.max_open_positions==0 and restored.risk.limits.allow_pyramiding
    assert set(restored.accounts.accounts["baseline"].positions)=={str(first.position_id),str(second.position_id)}
    eng.live.submit.assert_awaited()
    assert eng.live.submit.await_count==2 and eng.live.submit_stop.await_count==2

    original_second=second.model_dump()
    eng.live.fetch_verified=AsyncMock(side_effect=[
        {"status":"NEW", "executedQty":"0"},
        {"status":"CANCELED", "executedQty":"0"}])
    eng.live.cancel=AsyncMock()
    eng.live.submit_close=AsyncMock(side_effect=lambda intent: filled(intent).model_copy(
        update={"client_order_id":intent.decision_id.hex[:31]+"C"}))
    await eng._exit_live("baseline", "BTCUSDT", first, "TARGET", moment, eng.states["BTCUSDT"])
    account=eng.accounts.accounts["baseline"]
    assert str(first.position_id) not in account.positions
    assert account.positions[str(second.position_id)].model_dump()==original_second
    eng.live.cancel.assert_awaited_once_with("BTCUSDT", first_decision.decision_id.hex[:31]+"S")
    assert eng.live.submit_close.call_args.args[0].quantity==first.quantity
    assert account.trades[-1].decision_id==first.decision_id


@pytest.mark.asyncio
async def test_small_short_close_does_not_buy_another_positions_principal():
    eng = engine(market_type="margin")
    own = _open(eng, mode="live", side=Action.SHORT, quantity=.001,
                stop=110, target=90, stop_client_order_id=None)
    other = _open(eng, mode="live", side=Action.SHORT, quantity=.02,
                  stop=110, target=90, stop_client_order_id="other-stop")
    other.protection_status="PROTECTED"
    eng.feed.rules["BTCUSDT"]={"step_size":.001, "min_notional":1}
    eng.balance.snapshot=AsyncMock(return_value={"status":"ok","assets":[
        {"asset":"BTC","total":-.021},{"asset":"USDT","free":100}]})
    eng.live.submit_close=AsyncMock()
    state=eng.states["BTCUSDT"]
    state.best_ask=100
    state.last_price=100
    await eng._exit_live("baseline","BTCUSDT",own,"TARGET",clock(),state)
    eng.live.submit_close.assert_not_awaited()
    assert own.protection_error=="MIN_NOTIONAL_CLOSE_WITH_OTHER_POSITIONS"
    assert other.quantity==.02 and other.stop_client_order_id=="other-stop"
