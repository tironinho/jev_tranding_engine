# Daily risk reset persisted in PostgreSQL

POST /api/risk/reset-daily with {"confirm":"RESET"} uses the existing authenticated
engine API. It captures fresh real equity and a fixed UTC cutoff, appends them as
`daily_reset` in the PostgreSQL `risk_limits` event, and only then updates running
state. It leaves the configured daily loss/drawdown percentages unchanged and does
not erase trades. Later risk edits preserve daily_reset; boot restores it from the
latest saved risk_limits event. The cutoff applies only on its UTC day; new losses
count and subsequent restarts never move the cutoff.

If database persistence or real equity is unavailable, the reset returns 503 and
is not applied. Status exposes live_daily_risk_reset_at and
live_daily_risk_reset_equity. Environment variables introduced in the previous
implementation are no longer used. Production must run this code before calling
the endpoint. The loss-streak reset is a separate existing control.
