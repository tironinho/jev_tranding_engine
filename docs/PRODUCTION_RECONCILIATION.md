# Production reconciliation changes

This change requires the engine, account gateway, and web frontend to run the same revision. Deploy the account gateway first so fresh balance requests include every asset, principal borrowed, and interest. Pushing this branch does not deploy those services.

## Behavior

- Trades shows the complete margin account separately from strategy positions: cash, residual amounts, debt, unknown prices, and differences. It refreshes every 10 seconds while visible. The gateway balance cache remains bounded to 20 seconds for ordinary reads; execution requests explicitly bypass both caches.
- Material unmatched exposure or unknown prices blocks entries. Residual balances remain visible and contribute to exposure limits. No automatic sale or repayment of unrelated assets is introduced.
- Stops are reconciled before every exit. Uncertain order status blocks a second market order. Close IDs survive retries and restarts. Partial closes retain the remaining quantity, proportional entry fee, and locked margin. Amounts below the exchange minimum remain visible as residual positions.
- The entry intent is recorded before sending an order. Pending entries are recovered by their exchange client ID. The position is checkpointed before stop submission. Missing protection triggers an attempt to flatten; an unsuccessful attempt remains visible and blocks new entries. A failed database write disables further entries.
- Actual receipt/exchange timestamps are stored separately from snapshot time. Signal age, book age, and price movement are checked before sending an entry.
- Daily realized loss uses restored live trades and daily equity uses persisted account samples. After the holding horizon, only positions whose stop already protects total fee-adjusted break-even may continue toward the target; every unprotected position exits on the clock.
- Jev receives the candidate entry, stop, target, and horizon. Its exact successful HTTP request is recorded. Historical requests without this record are not reconstructed and presented as exact. Breaks cannot bypass the continuation floor; the strategy and risk engine require at least 0.15R of stressed expected net payoff and keep the assessed stop/target.
- Baseline defaults to paper for comparison. Existing explicit saved shadow/disabled settings remain respected; baseline cannot be restored into live mode.
- Decision detail falls back to PostgreSQL after a restart. No database schema migration is needed for the added JSON payload fields.

## Validation and remaining limits

197 engine tests passed, including cancel/fill races, ambiguous entry recovery, partial exits, stale book rejection, unavailable balances, residual exposure, and live breakeven stop replacement. Frontend production build and type checking passed. These checks use simulated exchange responses and do not place real orders.

The subsequent 2R target/breakeven configuration is retained. Live stop tightening now confirms cancellation and places the replacement on the exchange before recording the new stop; an ambiguous replacement triggers reconciliation/exit instead of leaving only a tighter local number.

Exchange commissions from FULL order responses are retained and converted when a quote is available. Otherwise the existing fee estimate is used. Interest is shown as an actual account liability, but historical interest and commissions are not fully allocated back to each trade; the trade PnL must still be treated as recorded/estimated, not fully reconciled exchange accounting. The patch does not backfill missing historic decisions or reconstruct missing execution timestamps.

Margin protection remains a stop-limit order, now labeled accurately. A gap can prevent its fill; local monitoring requires the engine to remain available. Existing exposure without an attributable order is displayed and blocks material new risk rather than being adopted with an invented entry price. Residual positions are not automatically liquidated. Probability calibration and a statistically meaningful paper comparison require collecting new observations; this patch does not claim improved profitability.
