from app.domain.enums import Action
from app.risk.economics import compute_trade_economics, size_quantity


def test_fees_and_net_rr_are_separate_from_gross():
    econ = compute_trade_economics(
        side=Action.LONG,
        entry=100,
        stop=98,
        target=110,
        quantity=1,
        entry_fee_rate=0.001,
        exit_fee_rate=0.001,
        exit_slippage_per_unit=0,
        funding_cashflow_total=0,
        fee_source="config",
    )
    assert econ.gross_risk == 2
    assert econ.gross_reward == 10
    assert econ.gross_rr == 5
    assert econ.costs.entry_fee == 0.1
    assert econ.costs.exit_fee_at_target == 0.11
    assert econ.costs.exit_fee_at_stop == 0.098
    assert econ.net_reward == 10 - 0.1 - 0.11
    assert econ.net_risk == 2 + 0.1 + 0.098
    assert econ.net_rr == econ.net_reward / econ.net_risk
    assert econ.net_rr < econ.gross_rr


def test_paying_funding_worsens_both_sides():
    base = compute_trade_economics(
        side=Action.LONG,
        entry=100,
        stop=98,
        target=110,
        quantity=2,
        entry_fee_rate=0,
        exit_fee_rate=0,
        exit_slippage_per_unit=0,
        funding_cashflow_total=0,
        fee_source="config",
    )
    paying = compute_trade_economics(
        side=Action.LONG,
        entry=100,
        stop=98,
        target=110,
        quantity=2,
        entry_fee_rate=0,
        exit_fee_rate=0,
        exit_slippage_per_unit=0,
        funding_cashflow_total=-0.5,
        fee_source="config",
    )
    assert paying.net_risk == base.net_risk + 0.5
    assert paying.net_reward == base.net_reward - 0.5


def test_size_uses_stop_distance_not_a_fixed_quantity():
    qty = size_quantity(
        equity=10_000,
        risk_fraction=0.005,
        entry=100,
        stop=98,
        entry_fee_rate=0,
        exit_fee_rate=0,
        exit_slippage_per_unit=0,
    )
    assert qty == 25
    with_fees = size_quantity(
        equity=10_000,
        risk_fraction=0.005,
        entry=100,
        stop=98,
        entry_fee_rate=0.001,
        exit_fee_rate=0.001,
        exit_slippage_per_unit=0,
    )
    assert with_fees < qty
    loss = with_fees * (2 + 100 * 0.001 + 98 * 0.001)
    assert abs(loss - 50) < 1e-6
