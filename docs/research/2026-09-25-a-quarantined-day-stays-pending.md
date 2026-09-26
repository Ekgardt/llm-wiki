# A quarantined day stays pending

Date: 2026-09-25. Audit item A-12 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on the live vault, read-only)

- The receipts under `knowledge/daily/receipts/` are the authority for "this
  day is compiled"; `compiled_daily_hashes` in `run/state.json` is a diagnostic
  mirror (module docstring of `scripts/compile_memory.py`).
- `select_dailies` still reads the mirror as a fallback: a day whose mirror
  digest equals its file digest is skipped even without receipts. That fallback
  is deliberate — vaults compiled before receipts existed keep their days
  (`test_exact_legacy_diagnostic_suppresses_migration_only_compile`,
  `test_a_day_the_discard_did_not_touch_keeps_its_record`).
- `_mirror_digests` seeded the mirror with every batch day's digest before
  looking at receipts, and `_record_batch_diagnostics` ran for a quarantined
  batch too. A quarantine commit writes candidates and no receipt, so the day's
  mirror digest matched its file and the fallback skipped it for ever. The
  message "the daily stays pending until the candidate is reviewed" was false.
- Live vault: `2026-09-01.md` is the one day the mirror calls compiled with no
  receipt. Its recorded commit (`compiled_daily_commits`, sequence 11291) is the
  transaction whose operation id starts `compile-quarantine:`; 4 of the 102
  candidates in `knowledge/inbox/claims/` come from it.

## Source

- Transactional outbox, https://microservices.io/patterns/data/transactional-outbox.html
  (fetched 2026-09-25): derived output is published "if and only if the
  database transaction commits". The mirror is derived from receipts; it may
  say only what a receipt says.

## Decision

- The mirror records a day only when every part carries a receipt
  (`_whole_daily_digest`); the per-part seed is removed. A quarantined batch
  therefore writes nothing to it.
- The per-pass repair also takes out a day whose recorded commit was a
  quarantine and which has no receipt, so the one poisoned day on an existing
  vault is offered again. The commit is looked up by its sequence
  (the transaction rowid) through a new read-only
  `MarkdownCoordinator.operation_id_at(sequence)`.
- The legacy fallback stays: a mirror-only day with no quarantine commit behind
  it is still left alone.
- What happens to a pending quarantined day is audit A-13's job; here it only
  stops being hidden.

## Files

- `scripts/compile_memory.py`
- `scripts/markdown_transaction.py`
- `tests/test_a_quarantined_day_stays_pending.py`
- `CHANGELOG.md`
