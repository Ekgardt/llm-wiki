# A transaction that is over stays over

Dated 2026-09-17. Findings M8, M9, M10 and M11 of the third audit (Markdown transactions).
The research before the fixes.

Files: scripts/markdown_transaction.py,
tests/test_recovery_does_not_revisit_a_finished_abort.py,
tests/test_a_nested_gate_waits_as_long_as_it_was_told.py,
tests/test_an_append_whose_images_were_pruned_can_be_asked_again.py,
tests/test_an_append_behind_a_stale_attempt_gives_up_in_time.py

## What was found

- M8. `_incomplete_transaction_rows` selects `aborted` rows on every recovery, sorts them
  ahead of `preparing/prepared/applying`, and `_validate_aborted` never retires one. Its
  check includes "every target still holds its before-state", which is true at the moment of
  the abort and false as soon as anyone legitimately writes the same path again — an intact
  receipt is then flagged `abort_receipt_invalid`. Bounded callers (session start takes 4)
  spend their whole budget on old aborted rows and never reach a crashed `applying` one.
  The cost of every `prepare` grows with every abort ever made.
- M9. `writer_gate(owner=..., wait_seconds=...)` ignores the wait: the nested gate makes one
  attempt and raises `owner_busy`. A project checkpoint that meets a running compile fails
  at once, although the caller asked to wait. The canonical gate has the wait loop
  (`_acquire_canonical_lease`); the nested one does not.
- M10. A duplicate append is recognised by re-reading the staged plan and after-image.
  `prune` removes both after the undo window and keeps the row. The same operation id asked
  again then raises `RuntimeError: transaction after-image is corrupt`, every time.
  Permanent ids exist: the daily header and the vault log header, written whenever the file
  is absent. Reproduced: append, prune at +3 days, unlink, append → RuntimeError.
- M11. In `_append_until_committed` the outcome `retry` neither advances the attempt nor is
  counted, and the loop never looks at the deadline. A `preparing` row whose pid answers as
  alive is never recovered, so the caller loops for good. Reproduced: with a one-second
  deadline the caller was still running after twenty.
- Code graph: `_incomplete_transaction_rows` ← `_recover_selected` ← `recover` ←
  `prepare`, session start, doctor, MCP. `_nested_writer_gate` ← `writer_gate` ← project
  checkpoint, compile, recover. `_classify_settled_append` ← `_settle_append_candidate` ←
  `_run_append_candidate` ← `_append_until_committed` ← `append_knowledge` and the capture
  append.

## Practice on this date

- On a key asked again after its record is gone, Stripe's API reference says: "You can
  remove keys from the system automatically after they're at least 24 hours old. We generate
  a new request if a key is reused after the original is pruned."
  ([Stripe API, Idempotent requests](https://docs.stripe.com/api/idempotent_requests)).
  Evidence that was deliberately pruned cannot make a later request an error; the request
  is simply new.
- Recovery is for work that can still move. A terminal state that is re-examined on every
  pass is not terminal, and a check that depends on the world not changing afterwards is a
  check of the wrong moment.

## The decision

- M8. Recovery takes work that can still move first: `aborting`, then
  `preparing/prepared/applying`. An `aborted` row is looked at last, only while it has no
  verdict yet (`error_code IS NULL`) and only inside the undo window
  (`UNDO_RETENTION_DAYS`) — the time in which a crash around the receipt can still matter.
  The receipt check no longer asks whether the targets still hold their before-state.
- M9. The nested gate waits like the canonical one: the same window
  (`_writer_wait_window`), the same backoff (`_writer_retry_delay`), only for `owner_busy`.
  When the window closes it raises the `owner_busy` it was given, so callers that handle
  that code keep working.
- M10. When the record's images were pruned, the request can no longer be compared, so it is
  neither a duplicate nor a conflict: the attempt advances, and the next candidate id
  carries a new write.
- M11. The append loop checks the caller's deadline and cancellation at every round, and
  counts the time it has spent without progress; past `_APPEND_STALL_SECONDS` it raises
  `TimeoutError`. The `preparing` row itself stays: it carries only a pid, with no start
  identity, so a reused pid cannot be told from the writer. Adding that column changes the
  coordinator schema, which is the owner's decision.
