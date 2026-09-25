# A transient transcript never outlives its event

Date: 2026-09-25. Audit item C-5 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- A host that sends transcript text instead of a path (OpenCode, Codex) has it
  written to `cache/transient-transcripts/` (`_materialize_event_transcript`) so
  the capture can read it as a file. `_cleanup_durable_transcript` removes the
  copy only when the capture intent was published: when publication fails the
  copy stays.
- The only reader of a kept copy was the `flush_memory.py` command line
  (`test_provider_and_queue_failure_preserves_ephemeral_transcript`), retired the
  same day (`docs/research/2026-09-25-the-flush-command-line-is-retired.md`).
  Nothing scans the directory again, so a kept copy is private session text in
  a cache for ever. The failed publication itself is recorded by the adapter's
  failure path. The live vault holds none today.

## Source

- GDPR Article 5(1)(e), https://gdpr-info.eu/art-5-gdpr/ (fetched 2026-09-25):
  personal data "kept ... for no longer than is necessary for the purposes for
  which the personal data are processed" — the purpose of the copy is the one
  capture it was made for.

## Decision

- The copy is removed when the event's handling ends, whether the intent was
  published or not.

## Files

- `scripts/integration_adapter.py`
- `tests/test_a_transient_transcript_never_outlives_its_event.py`
- `CHANGELOG.md`
