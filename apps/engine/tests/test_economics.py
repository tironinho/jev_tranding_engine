from app.domain.enums import Action
from app.risk.economics import compute_trade_economics, extend_target_for_costs, size_quantity


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


def test_a_fee_hair_below_the_minimum_extends_the_target():
    shared = dict(
        side=Action.LONG,
        entry=83559.88,
        stop=83350.98030000001,
        quantity=0.17102,
        entry_fee_rate=0.0005,
        exit_fee_rate=0.0005,
        exit_slippage_per_unit=0.0,
        funding_cashflow_total=0.0,
        spread_cost=0.0,
        slippage_cost=0.0,
    )
    before = compute_trade_economics(target=84082.12925, fee_source="config", **shared)
    assert before.net_rr is not None and before.net_rr < 1.5
    lifted, extended = extend_target_for_costs(target=84082.12925, min_net_rr=1.5, **shared)
    assert extended
    after = compute_trade_economics(target=lifted, fee_source="config", **shared)
    assert after.net_rr is not None and after.net_rr >= 1.5
    assert lifted - 84082.12925 < 83559.88 * 0.001


def test_minimum_stop_slippage_is_pushed_out_until_net_rr_clears():
    entry = 100.0
    distance = 0.25
    shared = dict(
        side=Action.SHORT,
        entry=entry,
        stop=entry + distance,
        quantity=10,
        entry_fee_rate=0.0005,
        exit_fee_rate=0.0005,
        exit_slippage_per_unit=entry * 8 / 10_000,
        funding_cashflow_total=0.0,
        spread_cost=0.0,
        slippage_cost=0.0,
    )
    target = entry - 2.5 * distance
    before = compute_trade_economics(target=target, fee_source="config", **shared)
    assert before.net_rr is not None and before.net_rr < 1.5
    lifted, extended = extend_target_for_costs(target=target, min_net_rr=1.5, **shared)
    assert extended
    after = compute_trade_economics(target=lifted, fee_source="config", **shared)
    assert after.net_rr is not None and after.net_rr >= 1.5
    assert target - lifted <= abs(target - entry)


def test_an_impossible_net_rr_does_not_move_the_target():
    lifted, extended = extend_target_for_costs(
        side=Action.LONG,
        entry=100,
        stop=98,
        target=108,
        quantity=1,
        entry_fee_rate=0.0005,
        exit_fee_rate=0.0005,
        exit_slippage_per_unit=0,
        funding_cashflow_total=0,
        spread_cost=0,
        slippage_cost=0,
        min_net_rr=50,
    )
    assert extended is False
    assert lifted == 108
