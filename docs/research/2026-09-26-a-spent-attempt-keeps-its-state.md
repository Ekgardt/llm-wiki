# A spent attempt keeps its state

Date: 2026-09-26. Audit 2026-09-26 item A-4 (a regression of the B-16 fix).

## Fact
- The B-16 fix (commit 871e7466) added a nightly step that turned every quarantined
  transaction with no applied operation into `discarded`
  (`markdown_transaction._SETTLE_ROLLED_BACK_QUARANTINES`).
- `attempt_operation_id` advances a retry to `#2` only while the previous record is
  `quarantined`; after the step it handed a refused compile its old, now
  discarded id, and the retry failed (`TransactionStateError` /
  `OperationBoundElsewhereError`): that day was never compiled again (audit
  reproduction).
- The step kept `run/transactions/<id>/`, and doctor requires a discarded row to
  have no directory: `transaction_metadata_corrupt` for about two days, until the
  prune caught up.
- Doctor's "refused attempt whose work never happened" finding reads quarantined
  rows; a settled row dropped out of it while the page still did not exist.
- The branch was never deployed: the live vault never ran the step.

## Source (fetched 2026-09-26)
Stripe API reference, "Idempotent requests", https://docs.stripe.com/api/idempotent_requests:
"Subsequent requests with the same key return the same result … The idempotency
layer compares incoming parameters to those of the original request and errors if
they're not the same." A spent operation id is a key; rewriting what it recorded
changes what every later retry of it gets.

## Decision
Remove the step (`settle_rolled_back_quarantines`, its SQL and its reclaim entry).
A quarantine stays what it is: the record that an attempt was refused, which the
retry chain and doctor both read. The part of the same commit that removes empty
`ready/` capture-intent shards (C-13) is correct and stays. The quarantine growth
B-16 described is bounded by refusals, which are rare; it stays open and is
recorded as such in the 2026-09-25 audit.

## Files
- scripts/markdown_transaction.py
- scripts/reclaim_runtime_state.py
- tests/test_a_rolled_back_quarantine_is_settled.py
- tests/test_a_spent_attempt_keeps_its_state.py
- docs/USER-GUIDE.md
- CHANGELOG.md
