# A publication that is out of time does not link

Dated 2026-09-17. Third audit, finding G-M7 (third point). The research before the fix.

Files: `scripts/evidence_graph.py`,
`tests/test_a_publication_that_is_out_of_time_does_not_link.py`.

## What was found

- `create_generation_database` takes `deadline`, `cancelled` and `monotonic` and honours
  them while it normalizes rows and while it writes them (`_check_build_stop`, the SQLite
  progress handler). `_published_database` — the last step, which validates the finished
  database and then hard-links it into place — was called with none of the three, so
  `validate_generation_database` ran unbounded.
- The validation reopens the database read-only and checks every contract in it. Measured
  by the audit at about 30 s on a vault-sized generation. So a build that was already out
  of time, or cancelled, paid that cost and then published: the caller's stop was ignored
  at exactly the step that makes the artifact visible to every reader.
- Deadlines are routine on this path: the nightly build runs under a step budget and the
  MCP `index` tool passes a deadline.

## Practice on this date

- SQLite, "Atomic Commit In SQLite": "the rollback journal file is deleted … This is the
  instant when the transaction commits."
  (https://www.sqlite.org/atomiccommit.html, section 3.11 — a commit is the deletion of the
  journal). That is why the test can tell the built state from the writing state without
  patching anything: the temporary database exists and its `-journal` does not.
- `link(2)`: "creates a new link (also known as a hard link) to an existing file"
  (https://man7.org/linux/man-pages/man2/link.2.html). It is the publication's point of no
  return, and it comes after the validation, so a stop raised during the validation leaves
  only the temporary file — which the existing `except BaseException: _discarded_temporary`
  already removes.

## The decision

- `_published_database` takes the three stop parameters and passes them to
  `validate_generation_database`, which already accepts and honours them. Nothing else
  changes: the byte ceiling, the link and the unlink stay where they are, and a stop after
  the link is still impossible because nothing between them can raise a stop.
- The test drives the product path with no patching: a cancellation that fires on the ask
  after the build's own last one (the journal is gone, the temporary database is there, and
  the build asks exactly once in that state). Before the fix that build published a
  database while a cancellation was pending; after it, the call raises `TimeoutError` and
  neither the database nor the temporary file is left behind.
