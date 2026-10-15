# Capital allocation

New entries use the smallest quantity allowed by stop risk, existing capital checks,
and the unleveraged allocation budgets in packages/configs/risk.yaml:

- max_trade_notional_fraction: 0.30. All open entries on the same symbol share this
  budget, preventing pyramiding from bypassing it.
- max_minimum_trade_notional_fraction: 0.40. For a symbol with no open exposure,
  the first entry may exceed the normal 30% only up to the smallest executable
  quantity, bounded by 40% of equity. The calculation includes entry fees, lot
  rounding and the lowest planned exit price with the existing 1% exit buffer.
  Set this value to zero to disable the exception. It is exposed and editable
  through the risk limits API and persisted in the existing database settings.
- max_portfolio_notional_fraction: 0.80. Gross open exposure counts against this
  ceiling without leverage multiplication. This leaves exposure headroom; it is
  not a guarantee of free cash or a liquidation buffer.
- min_net_target_return: 0.005. Net reward at the planned target divided by entry
  notional, after the existing fee, slippage and interest calculations.
- min_net_rr: 2.0. Minimum net reward/risk at the planned target.

No targets are extended to pass these new filters. A minimum-sized exchange order
cannot bypass portfolio, cash, symbol exposure or stop-risk ceilings. The exception
cannot fund repeated entries on a symbol with existing exposure. Rejections report required/available notional
and the limiting capital check. Existing positions retain their protection.

The policy does not rank simultaneous signals, estimate calibrated win probabilities,
or allocate correlation groups. It caps concentration and filters target economics;
net target reward is not expected profit. Limits are exposed by GET /api/risk/limits
and loaded at engine startup. No running production process was restarted.

Validation: 431 engine tests passed. Historical profile helpers explicitly retain
legacy allocation and reward/risk values for their regression cases.
