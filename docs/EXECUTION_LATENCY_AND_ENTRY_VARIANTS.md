# Execution latency and prospective entry variants

## Execution path

Position management now precedes research-label persistence in each quote batch.
Snapshots are persisted concurrently with strategy evaluation. Every strategy waits
for the snapshot write before persisting its decision, and live orders retain the
existing persistence, freshness, balance and order-intent gates. Slow writes can
still expire a signal; nothing opens an order ahead of its required durable data.
The change overlaps independent work rather than discarding persistence.

For live positions, the first locally observed executable quote at TARGET/STOP is
captured on market ingress. Position state and resulting trades preserve
exit_observations/exit_timing, including event time, local receive time, quote,
management start, order submission, response and exchange time where available.
Touch-to-submit, management-to-submit and submit-to-response timings are reported.
This is a local observation, not a claim about the first crossing on the exchange.
Missing observations remain absent; no historic timings are fabricated. Stop
replacement clears the old stop observation. No stop/target prices or quantities
are changed by this instrumentation. The pre-close order checkpoint remains.

JEV telemetry separates queue_ms, provider_total_ms, effective timeout_s and
failure/transport exception classes. Provider total includes HTTP client work and
local scheduling: it is not pure model inference time. Network connection/read
phases are not independently timed in this patch. Timeouts are not increased.

## Entry research

role_baseline_v2 records immediate, confirmed_retest and sustained_reversal entry
variants in SHADOW. Retests use the level frozen before the break; the old rolling
extreme included the break, making earlier-close retests logically unreachable.
A qualifying retest returns near that old level within 0.5 ATR and closes on its
favorable side, without intervening closes invalidating the level.

A sustained reversal requires the existing reclaim plus two closed 5m closes back
on the recovered side of the reference level. Neither variant authorizes orders.
The selection-review endpoint groups realized trade outcomes by snapshot variant
eligibility and version; this is observational, not simulated PnL from waiting for
a different entry. Subsequent shadow sampling is required for a fair prospective
comparison. No SOL ban or unconditional 1m/5m-flow veto was introduced.

The 30% symbol/80% portfolio budgets and 2:1 net payoff floor remain unchanged.
This patch does not remove CPU constraints, increase infrastructure cost or prove
that provider-side timeouts disappear. It supplies measurements and removes known
avoidable delays. Existing trades retain their plan and emergency protection.
