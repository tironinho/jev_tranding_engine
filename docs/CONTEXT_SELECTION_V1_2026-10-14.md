# Context selection v1

This implements the prospective selection experiment from the 10/20-trade audit.
Production configuration defaults to `entry_selection_mode: shadow`. Changing the
file to `enforce` and restarting applies the same recorded criteria; invalid modes
fail configuration loading. Shadow leaves the existing execution gates active and
records the proposed selection on decisions and risk results. It is not a separate
simulated portfolio. New JEV requests receive additional real context immediately
when this version is deployed, even while the selection filter remains in shadow.

## Evidence and setup

The seven-group JEV contract now includes context_15m_slope_pct,
distance_to_vwap_pct, return_5m_pct, return_15m_pct and return_60m_pct, plus a
candidate-direction reclaim flag. All percentages multiply source fractions by
100; absent/nonfinite numeric evidence remains absent. The existing EMA slope
field measures five one-minute bars, distinct from the added 15-minute context.

`context_entry_v1` labels CONTINUATION for context aligned with the side and
REVERSAL otherwise. Continuation requires aligned 5/15m returns, both executed
flow windows and position relative to VWAP. A counter-context reversal additionally
requires the candidate-direction reclaim. Missing/NaN evidence yields UNKNOWN,
not presumed agreement. This is a provisional research policy, not a calibrated
predictor; no thresholds were fitted to the recent losing sample.

## Repeated entries

When an owned symbol position exists, a proposed additional entry needs a structural
break or reclaim, at least five minutes since the prior entry, and at least 0.5 ATR
of favorable price progress beyond every owned entry price. This avoids treating
adjacent one-minute readings as distinct evidence. It is a conservative novelty
proxy, not a tracked multi-bar retest detector. Existing live opposite-side and
futures close-all restrictions remain active. Limits are exposed by /api/risk/limits.

The existing 30% symbol and 80% gross portfolio budgets still apply. This change
does not estimate cross-symbol correlations or add a SHORT-only production mandate.

## Prospective review

GET /api/jev/selection-review reports observed live ledger cohorts by build commit,
prompt, side, selection version, mode, setup and would_accept. It records fees and
net results; Brier and reliability buckets include only TARGET/STOP exits whose
assessed target and stop match the recorded trade. Emergency/time exits, missing
probabilities, missing details and absent selection telemetry are counted explicitly.

The endpoint uses locally available inspector records; it reports gaps instead of
claiming complete database history. There is no fitted calibration, counterfactual
portfolio profit, automatic deployment or automatic switch to enforce. Independence
and continuous observation still require checking before drawing conclusions.

Cost-aware target assessment, capital budgets and disabled timed exits remain in
place. No existing stops/targets were changed. Production was not restarted here.
