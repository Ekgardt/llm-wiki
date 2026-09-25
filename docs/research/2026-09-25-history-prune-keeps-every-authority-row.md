# The history prune keeps every row something reads back

Date: 2026-09-25. Audit item A-4 of `docs/AUDIT-2026-09-25-full.md` (a regression
of 2026-09-24) and C-15.

## Question

The prune added on 2026-09-24 deletes settled transaction rows older than 90
days, except the ones a project checkpoint names. Compile receipts, daily archives
and evidence resolution read compile transactions back by `operation_id`, and an
archive's attestation also carries the row's commit sequence. Which rows may a
prune remove?

## Sources

- Stripe API reference, "Idempotent requests" (fetched 2026-09-25,
  https://docs.stripe.com/api/idempotent_requests): idempotency keys may be removed
  once they are at least 24 hours old, and a key reused after pruning makes a new
  request. A record kept only for replay detection has a window; a record other
  records point at does not.
- `docs/research/2026-09-24-every-store-has-a-bound.md`: the 90-day window and
  the measured growth (23 557 rows).

## Findings (facts)

1. Readers by `operation_id`: `compile_memory._require_transaction_authority`
   (`:948`) and `:3200`, `:3687` (compile receipts), `archive_daily.py:440`, `:851`
   (daily archives), `evidence_resolver.py:497` plus `_commit_sequence` (the bag's
   compile authority attestation), and the coordinator's own replay checks
   (`markdown_transaction.py:3659`, `:3845`, `:4280`, `:4317`, `:5515`-`:5632`).
2. The live coordinator holds 23 457 committed rows by operation family:
   `post-tool` 14 863, `project` 6 595, `user-prompt` 1 540, `session-evidence`
   306, `compile` 80, `repair-backlink` 61, `episodes` 40, `capture-markdown` 27,
   `daily-header` 24, the rest under 20 each.
3. `post-tool` and `user-prompt` rows are breadcrumbs appended by hooks; nothing
   reads them back except the replay check of the same hook event, which arrives
   within seconds.

## Decision (conclusion)

- The prune removes only rows of the breadcrumb families `post-tool` and
  `user-prompt`, past the 90-day window, settled, with images already pruned, and
  not named by a checkpoint. Every other family is authority for something that
  outlives it (a receipt, a bag, a capture terminal record, a page) and is kept.
- This keeps the bound where the growth is: the two families are 16 403 of 23 457
  rows.
- A replay of a breadcrumb event after 90 days would be applied again, which is
  the Stripe behaviour for a pruned key; no hook replays an event that old.

## Edited files

- `scripts/markdown_transaction.py`
- `tests/test_every_store_has_a_bound.py`
