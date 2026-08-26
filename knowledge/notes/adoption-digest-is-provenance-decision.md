---
type: decision
status: accepted
date: 2026-08-26
confidence: high
source_authority: ai-derived
---

# The adopted producer digest is provenance, not a standing precondition

One-sentence summary: the Reliability V3 adoption record keeps naming the
`scripts/integration_adapter.py` bytes that performed the cutover, but that
digest is no longer re-checked on every memory write — it is bound where it
decides something, in the window between planning and applying the migration,
and after that the producer only has to still be installed.

## What the check was, and what it cost

Adoption freezes `installed_integration_sha256` in
`run/reliability-v3-migration.json` and `run/reliability-v3-adopted.json`.
Two places compared it against the file on disk:

- `_validate_migration_context` — during adoption only.
- `_require_adoption_sources` — on **every** validation, forever.

The second one is reached from `require_reliability_v3_adopted`, and that guard
stands in front of the whole memory write path: session capture, the queue, and
every Markdown transaction. So after the cutover, the first byte changed in
`scripts/integration_adapter.py` would stop the vault remembering anything, with
`ReliabilityV3ValidationError: reliability_v3_record_invalid` and nothing saying
why.

That is not hypothetical. The owner approved unattended nightly self-update of
this checkout on 2026-08-23 ([[knowledge/notes/automatic-code-update-decision]]).
The first nightly fast-forward touching the adapter would have disabled memory
silently, at 03:00, with no one watching. Ordinary editing does it too: this
file was modified by another agent twice on the day of the cutover attempt.

Measured on a temp vault adopted through the real entry point, before the fix:

```
adopted: ok
after producer update: REFUSED -> reliability_v3_record_invalid
```

## Why removing it gives up nothing

A digest answers "different". It cannot answer "incompatible", which is the
question that matters. The incompatibility that a reader must fail closed on is
the **shape of the adopted databases**, and that is checked separately and still
is: `schemas` carries `queue_schema_sha256`, `coordinator_schema_sha256` and
`adoption_schema_sha256`, and `_require_adoption_sources` still refuses when any
of them drifts from what the running code expects.

Nor does the permanent check defend against a downgrade. Code old enough not to
know about V3 never calls `require_reliability_v3_adopted` at all; it would open
the tombstone and fail on its own. The digest was never what stood in its way.

What is kept:

- **The migration window stays closed.** `_validate_migration_context` still
  refuses an adoption whose plan was recorded against different adapter bytes,
  so `--plan` and `--apply` cannot straddle an edit.
- **Both records must still agree.** `_require_adoption_header` compares
  `installed_integration_sha256` between the migration and the adoption record,
  so the frozen provenance stays internally consistent forever.
- **The producer must still exist.** `_require_adoption_sources` now refuses
  when `scripts/integration_adapter.py` is missing. An absent producer is a
  broken installation and is checkable; a changed one is an ordinary update.

## What was rejected

Re-recording the adoption after every update. It was the obvious alternative and
it is worse. The adoption record is create-only immutable evidence by contract,
so "re-record" means either violating that or adding a second, mutable record
carrying a current digest that no reader could act on — a number nobody compares
to anything. It would also put a write into the nightly pass whose only job is
to silence a check that had already been shown to decide nothing.

## Open questions

- The producer is checked for presence, not for being the *right* producer. A
  vault whose `scripts/` is replaced wholesale with a different product would
  pass this check and fail later on schema digests. That is the intended order:
  fail on the thing that is actually incompatible.

## Source / Evidence

- `scripts/installed_memory_repair.py` — `_require_adoption_sources`,
  `_validate_migration_context`, `_require_adoption_header`.
- `tests/test_adoption_producer_and_tombstone_readers.py` —
  `test_a_changed_producer_does_not_invalidate_an_adopted_vault` and
  `test_a_missing_producer_still_fails_closed`; both fail on the code before
  this change.

## Related

- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]] —
  the approved adoption this refines.
- [[knowledge/notes/automatic-code-update-decision]] — the unattended update that
  made a permanent digest a kill switch.
- [[knowledge/notes/self-resolving-health-findings-decision]] — the same rule in
  another place: a condition nobody can clear is a condition nobody reads.
