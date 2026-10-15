# Prospective opening optimization v3

role_baseline_v3 adds CONTINUATION_PULLBACK without requiring a new breakout.
A pullback resumes on a closed 5m bar: the previous close moved against direction,
the latest close reverses that movement with a matching candle body. The variant
requires aligned 15m slope and price/EMA15m evidence and favorable 1m executed
flow. The 5m flow must be known; disagreement stays explicit and reduces ranking,
rather than becoming an unconditional veto. This setup remains SHADOW and cannot
authorize orders. No winner-derived probability or RSI cutoff was introduced.

Context votes remain exposed. Majority agreement with a contrary vote is marked
CONTINUATION_WITH_CONFLICT, with explicit context_slope_against and warnings.
Timing compares immediate, immediate_not_extended, confirmed_retest,
continuation_pullback and sustained_reversal variants. Extended/decelerating
entries record RETEST as a research preference. These are eligibility observations
at a snapshot, not simulated alternative fills or proof of profitability.
The JEV trend group also receives price_vs_ema20_15m_pct, completing the three
context observations (slope, EMA distance, VWAP distance), without hiding conflict
behind a single summarized label.

## Local order preflight

The active single-plan LIVE path checks necessary entry/exit notionals before
calling JEV. It uses exchange step/minimum rules, the assessed plan, fees and the
fresh local account cache. No HTTP request or database access is added. A missing
or expired balance/rules/plan produces UNKNOWN and does not prevent evaluation.
Paper/shadow research and optional multi-plan policies retain their original paths.

The upper quantity is calculated using existing individual and portfolio ceilings,
with 1% optimistic slack to avoid borderline cache/mark differences becoming a
hard refusal. Entry and exit quantities are rounded down to the exchange step;
LONG exit quantity includes estimated asset commission. Lowest planned exit/stop
prices and the existing exit-minimum buffer are checked. If even this optimistic
bound cannot clear a minimum, evaluation returns PREFLIGHT_ENTRY_BELOW_MIN_NOTIONAL
or PREFLIGHT_EXIT_BELOW_MIN_NOTIONAL, with numeric diagnostics and jev_effect=not_called.

NOTIONAL_POSSIBLE is not authorization. Remaining capital, exact fill, margin,
protection, liquidity, risk, fees and freshness are still validated before execution.
The preflight neither reserves capital nor adjusts targets, quantities or limits.
The 30% symbol/80% portfolio budgets, 2:1 net floor and 0.45 baseline gate remain.
No ban on SOL or automatic increase in JEV confidence was introduced.
