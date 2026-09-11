# A dropped best-effort write is counted, and one bound is declared once

Date: 2026-09-10. Trigger: audit findings OPS-21 and OPS-10.

OPS-21: six best-effort writes drop their failure with `except Exception:
pass` (or a fallback value): the capture-operation state (`capture_operation.py`,
two sites), feedback capture in `flush_memory._capture_feedback`, and the
three telemetry emitters in `mcp_server` (page reads, decision impressions,
context injection). Each is defensible alone — a lost race is not a hook
failure — but none increments a counter, so the doctor's capture-loss check
cannot see them.

OPS-10: `scheduled_nightly.REPOSITORY_REFRESH_BUDGET_SECONDS` (15 min) is
the subprocess kill timeout of the `repository_index.py refresh-all` step,
and `repository_index.REFRESH_ALL_BUDGET_SECONDS` (15 min) is the child's
own deadline that defers a repository "never half-built". The child's clock
starts after interpreter start-up, so the parent's kill lands first and the
graceful deferral can never run.

## Sources

1. `knowledge/notes/observable-capture-and-bounded-maintenance-decision.md`
   and `capture_diagnostics.record_capture_failure`: a failed capture is
   recorded, with `deferred` for a writer race and `lost` otherwise, in the
   trail and the counters the doctor and session start read.
2. `repository_index.py`: `refresh-all --budget-seconds` already exists; the
   child defers what does not fit and reports it.
3. The compile-lock note of this evening: one bound, declared once, with
   the outer wait strictly larger than the inner one.

## Decision

1. The six sites call `record_capture_failure(kind, reason, error=exc)`
   with kinds `capture_operation_state`, `feedback_capture` and
   `telemetry_event`; behaviour is otherwise unchanged (the hook still never
   fails).
2. The nightly passes `--budget-seconds` from `repository_index.REFRESH_ALL_BUDGET_SECONDS`
   to the child and bounds the step at that budget plus
   `STEP_START_MARGIN_SECONDS` (120 s: interpreter start-up, the last
   repository's deferral and the report). The nightly's own restated
   constant is gone.

Files: `scripts/capture_operation.py`, `scripts/flush_memory.py`,
`scripts/mcp_server.py`, `scripts/scheduled_nightly.py`,
`tests/test_scheduled_nightly.py`, `tests/test_capture_failure_is_recorded.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
