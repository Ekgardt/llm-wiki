# The legacy sweep checks every caller

Date: 2026-09-26. Audit 2026-09-26 C-15.

## Facts

Checked with the code graph (`trace_path` inbound,
tests included) and `grep` over `scripts/ integrations/ benchmark/ install.* skills/
rules/` of the worktree on 2026-09-26:

- `rebuild_memory_index._published_notes` and `benchmark/run_scale_matrix._flat_index_metadata`:
  no caller anywhere. `NOTES_DENIAL` was used only by the first.
- `flush_memory._capture_time_text`: already gone from the worktree (the graph
  indexes the main checkout, which is behind).
- `tests/test_security_invariants.py::TestTranscriptPathContainment` asked
  `flush_memory._transcript_path_allowed`, removed earlier, behind `hasattr`, so it
  passed without testing anything; the capture path is validated today by
  `integration_adapter._validated_capture_transcript_path`.
- `MarkdownCoordinator.abort_for_discard`: 8 callers, all tests; it is the only
  writer of the `aborting` state whose recovery (`_recover_aborting`) is live code.
  `MarkdownCoordinator.deletion_blockers` (4 test callers) and
  `operational_ownership.acquire_compile_owner` (3 test callers, the pre-adoption
  compile owner) are the tests' harness for those paths.
- Python's documentation of `hasattr`
  (https://docs.python.org/3/library/functions.html#hasattr, fetched 2026-09-26)
  says "The result is True if the string is the name of one of the object's
  attributes, False if not" — a test guarded by it silently does nothing once the
  name is gone.

## Decision

- Removed: `_published_notes`, `NOTES_DENIAL`, `_flat_index_metadata`.
- The transcript containment tests now call the live validator and expect a
  refusal (`OSError`; `PermissionError` naming the extension for a wrong suffix).
- Kept, with the reason above: `abort_for_discard`, `deletion_blockers`,
  `acquire_compile_owner`. Removing them would take live recovery code or the
  tests that hold it with them.
- The `guardrails_block` docstring no longer names the retired feedback candidates.

## Files

- `scripts/rebuild_memory_index.py`
- `benchmark/run_scale_matrix.py`
- `scripts/session_start_context.py`
- `tests/test_security_invariants.py`
- `CHANGELOG.md`
