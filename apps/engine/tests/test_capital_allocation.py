import pytest
from app.config import RiskLimits
from app.service import TradingEngine
from app.domain.enums import Action
from app.risk.engine import FeeQuote
from tests.conftest import engine, settings, long_snapshot
from tests.test_risk_limits import _context, _order


def allocated_engine():
    eng = engine()
    eng.risk.update_limits(max_trade_notional_fraction=.30, max_portfolio_notional_fraction=.80)
    return eng


def test_production_budget_is_independent_of_leverage():
    eng = TradingEngine(settings())
    assert eng.risk.limits.max_trade_notional_fraction == .30
    assert eng.risk.limits.max_portfolio_notional_fraction == .80
    assert eng.risk.limits.min_net_target_return == .005
    assert eng.risk.limits.min_net_rr == 2


@pytest.mark.parametrize("leverage", [1, 5, 10])
def test_trade_and_repeated_symbol_share_unleveraged_budget(leverage):
    eng = allocated_engine()
    eng.risk.update_limits(max_leverage=leverage)
    room, _ = eng.risk._capital_room(_context(equity=100, cash=100), .001)
    assert room == pytest.approx(30)
    room, _ = eng.risk._capital_room(_context(equity=100, cash=100, symbol_exposure_notional=25), .001)
    assert room == pytest.approx(5)


def test_other_positions_leave_only_remaining_portfolio_budget():
    eng = allocated_engine()
    room, reason = eng.risk._capital_room(_context(equity=100, cash=100, total_exposure_notional=75), .001)
    assert room == pytest.approx(5)
    assert reason == "MAX_PORTFOLIO_NOTIONAL"


def test_minimum_order_cannot_bypass_allocation_cap():
    eng = allocated_engine()
    snapshot, book = long_snapshot()
    result = eng.risk.evaluate(_order(snapshot, Action.LONG), snapshot,
        _context(equity=10, cash=10, day_start_equity=10, step_size=.001, min_notional=5),
        FeeQuote(.0002, .0005, "test"), book)
    assert "ORDER_BELOW_MIN_NOTIONAL" in result.reject_reasons
    assert result.details["available_notional"] == pytest.approx(3)


def test_tiny_net_target_is_rejected_without_moving_target():
    eng = engine()
    eng.risk.update_limits(min_net_target_return=.50)
    snapshot, book = long_snapshot()
    result = eng.risk.evaluate(_order(snapshot, Action.LONG), snapshot, _context(),
        FeeQuote(.0002, .0005, "test"), book)
    assert "NET_TARGET_RETURN_TOO_LOW" in result.reject_reasons
    assert result.economics is not None
