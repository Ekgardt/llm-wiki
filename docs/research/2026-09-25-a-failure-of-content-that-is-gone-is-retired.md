# A failure of content that is gone is retired

Date: 2026-09-25. Audit item B-15 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on the live queue)

- A failed compile records one `source_failures` row per daily source, keyed by
  `(logical_path, source_digest)`, the digest of the exact bytes compiled
  (a whole day or one part of it). A later commit clears only the rows of the
  digests it compiled (`_clear_compile_source_failures`).
- A daily log is append-only: a new capture changes its bytes and so its digest.
  The row for the old digest is then about content that no longer exists, and
  no commit will ever name that digest again.
- Live queue, 2026-09-25: 45 rows; 44 name a digest that is neither the day's
  current whole-file digest nor any of its current part digests; 1 is current.
  Such rows block archiving that day (`archive_daily`: `source_failure`) and,
  through `doctor`, the `run/` deletion contract, for ever.

## Source

- HTTP semantics, RFC 9110 §8.8.1,
  https://httpwg.org/specs/rfc9110.html#weak.and.strong.validators (fetched
  2026-09-25): "A strong validator is representation metadata that changes value
  whenever the representation data changes." A content digest is such a
  validator; a record keyed by one is about that representation only.

## Decision

- Each compile pass lists the failure rows and retires every row whose digest is
  not one of the current digests of its path (the whole file and each part the
  compile would cut it into); a row whose file is gone is retired too. The row
  for content that still exists stays until a commit of it clears it.
- The queue gains one read (`source_failure_keys`) on both backends; deletion
  uses the existing `clear_source_failure`.

## Files

- `scripts/memory_queue.py`
- `scripts/compile_memory.py`
- `tests/test_a_failure_of_content_that_is_gone_is_retired.py`
- `CHANGELOG.md`
