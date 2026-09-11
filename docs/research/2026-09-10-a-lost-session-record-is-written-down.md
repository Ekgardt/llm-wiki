# A lost session record is written down

Date: 2026-09-10. Trigger: audit finding H5. `session_evidence.write_session_evidence`
catches every exception and returns `None`; its own docstring records that
this swallow hid every queued session between 2026-08-24 and 2026-08-26.
The callers (`flush_memory`, `backfill_sessions`) ignore the `None`, so a
refused lease, a DLP refusal or an oversize record leaves no trace in
`logs/`, in the doctor, or in the capture diagnostics.

## Sources

1. `knowledge/notes/observable-capture-and-bounded-maintenance-decision.md`
   (owner-approved): "a failed capture is recorded durably instead of
   vanishing". The mechanism is `capture_diagnostics.record_capture_failure`:
   one bounded JSONL trail (`logs/capture-failures.jsonl`) plus counters in
   `run/state.json`, read by the doctor and the session-start block; it never
   raises, and with the exception in hand it says whether the write was
   lost or deferred by a writer race.
2. The four hook producers (`session_end_capture`, `precompact_capture`,
   `user_prompt_capture`, `post_tool_capture`) and `integration_adapter`
   already call it; the session-evidence writer is the one producer of
   durable capture that does not.

## Decision

`write_session_evidence` keeps its no-raise contract and, on failure, calls
`record_capture_failure("session_evidence", "<Type>: <message>", error=exc,
session_id=...)` before returning `None`. Nothing else changes: the trail
and the counters are the existing ledger, and the doctor already reads it.

Files: `scripts/session_evidence.py`, `tests/test_session_evidence.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
