# A deferred dense leg says "deferred" — the premise did not hold

Date: 2026-09-10/11. Trigger: audit finding M1: "under any deadline the
legacy dense leg is disabled and the trace blames the model; every MCP call
carries a deadline, so on a vault without an active generation the
semantic leg never runs".

## What was checked (2026-09-11)

- `search_memory._legacy_dense_hits` does return `None` when called with a
  `deadline`, before probing the model (a deliberate rule since 2a81516,
  asserted by `test_legacy_dense_skips_model_and_lance_under_hard_deadline`).
- `retrieval.retrieve_via_search_memory` sets `hard_deadline = deadline_monotonic
  is not None` and then `optional_deadline = _optional_value(hard_deadline,
  deadline_monotonic)`, which is `None` whenever a deadline was given: an
  optional stage is bounded by `_call_dense`'s stage timeout, not by
  passing the deadline down. So on the product path the legacy leg receives
  `deadline=None` and runs; the guard reaches only direct callers of
  `_legacy_dense_hits` that pass a deadline themselves.
- A test written to reproduce the audit's claim (`search(...,
  deadline_monotonic=...)` on a vault without a generation, legacy dense
  patched to fail if called) failed the other way: the legacy leg ran.

## Decision

No product change: the trace does not blame the model for a deferred leg
on the product path, because the leg is not deferred there. The docstring
of `_legacy_dense_hits` now states what the guard reaches. M1 is recorded
as verified-not-a-defect.

Files: `scripts/search_memory.py`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
