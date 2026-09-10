# An outcome is named, not summarised — 2026-09-10

Three findings from the users' issues (#26.2, #26.3, #29.5) share one
defect: a surface collapses distinct outcomes into one word. `compile` prints
`done` and records `ok` whether it published pages or only quarantined a
candidate; the capture health counts a lost writer race as a lost session;
the doctor says `requires operator attention` without the reason a claim's
evidence stopped resolving or the repair.

**What the field does.**

- Kubernetes API conventions: a status carries typed `conditions`, each
  with `reason` (a machine-readable camel-case word) and `message` (for a
  person); a bare `phase` is discouraged precisely because it hides which of
  several states the object is in.
  https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md#typical-status-properties
- Google SRE, "Monitoring Distributed Systems": a page must be actionable;
  a signal that fires on a condition the operator cannot act on is noise
  and trains people to ignore the surface.
  https://sre.google/sre-book/monitoring-distributed-systems/
- AWS Builders' Library, "Timeouts, retries, and backoff with jitter": a
  retryable error is not a failure until the retry budget is exhausted;
  counting each attempt as a failure inflates error rates and hides real
  ones. https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/

**What we keep.** `scripts/compile_memory.py` already distinguishes the
two commits by operation id (`compile-quarantine:` versus the page commit),
and a quarantined daily already stays selected for the next run because the
receipt predicate, not the mirror, decides (`tests/test_compile_transactions.py`
pins it). `scripts/integration_adapter.py` already names checkpoint
contention apart in the hook error log, and `scripts/doctor.py` already
excludes those kinds from the hook count. `scripts/claims.py` already
records one code per unresolved claim in `claim_index_diagnostic`.

**Decision.**

- Compile records each batch's outcome (`published` or `quarantined`, with
  the count of pages or candidates), prints it per batch, and finishes with
  `last_compile_outcome` in state: `published`, `quarantined`, `partial`
  (both), `nothing`, or `failed`. `last_compile_status` keeps meaning "the
  run completed" so nothing that reads it changes.
- `scripts/capture_diagnostics.py` classifies a reason at record time with
  the same contention markers the adapter uses (moved here so both read one
  list). A contended write is recorded as `deferred`, counted apart, and
  never enters `lost`; the doctor reports both numbers.
- The doctor's claim check names each code with its cause and the repair,
  and lists the pages, bounded.

**Limits.** Records written before today carry no outcome and count as
lost, as before. The quarantine review path is documented as it is: the
candidate sits under `knowledge/inbox/claims/`, the daily stays pending,
and there is no accept command; publishing a reviewed decision is a
transaction the operator writes.
