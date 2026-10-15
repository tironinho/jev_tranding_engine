# Protective-stop failure after automatic repayment — 2026-10-15

The XRP LONG opened at 11:44:20 Brasilia time (14:44:20 UTC), decision
`d9249e30-41bf-4b7f-8b5e-e35aab0c7ce3`, filled 4.1 XRP at 1.3194.
The protective SELL for 4.1 XRP was rejected for insufficient balance.
Three fresh account reads returned 4.03713269 XRP free, which rounds down
to 4.0 XRP at the exchange's 0.1 step. The engine correctly refused to label
a smaller quantity as full protection and closed 4.0 XRP at 1.3187. The
recorded remainder was 0.1 XRP and the exit reason was PROTECTION_FAILED.

This was not explained by a stale account read. Before entry, the persisted
account sample had 0.40599 XRP free and -0.06286731 XRP net, implying
0.46885731 XRP in liabilities. The order response charged commission in BNB.
The entry used AUTO_BORROW_REPAY, so the received XRP paid the older debt:

    0.40599 + 4.1 - 0.46885731 = 4.03713269 XRP free

Sources are the production account sample at 14:44:11 UTC, the entry fill,
and the engine's live_stop_balance logs at 14:44:21–22 UTC. The observed
numbers match Binance's documented automatic repayment behavior:
[sideEffectType documentation](https://www.binance.com/en/support/faq/detail/f9fc51cda1984bf08b95e0d96c4570bc).

## Change

Margin entry orders use MARGIN_BUY (automatic borrowing without automatic
repayment). This preserves the received asset for the new entry's full
protective order, for both BUY and SELL entries. Closing orders and
protective stops retain AUTO_REPAY. Existing liabilities can therefore
remain outstanding while an entry is open; this is intentional, and account
liabilities and interest must continue to be included in risk calculations.

The account gateway accepts MARGIN_BUY for entries and retains
AUTO_BORROW_REPAY entry compatibility for an older engine during rollout.
STOP_LOSS_LIMIT requests must use AUTO_REPAY, so a protective stop cannot
gain borrowing authority through the gateway. The opposite-position gate,
entry sizing, full-quantity protection requirement, deterministic IDs,
timeout reconciliation, and emergency-close behavior remain in force.

## Verification and rollout

The regression test models the incident's existing free balance and debt,
borrowing, an entry fill, a full protective stop, cancellation, and repayment
on close. It covers both entry directions. Gateway tests cover new and legacy
entry modes and rejection of borrowing stop orders. The new regression tests
failed on the old implementation before the change.

Deploy the account gateway before the engine: the older gateway rejects
MARGIN_BUY. Both services use the same repository but are separate processes.
Compatibility with old entries avoids needing to change their current
protection or exit behavior. No exchange order was placed to test this fix.

Implementation and tests are local until explicitly published. This document
does not claim the production deployment has been changed.
