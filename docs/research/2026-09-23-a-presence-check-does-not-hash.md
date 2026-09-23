# A presence check does not hash

Dated 2026-09-23. The one red job on `main` after PR 36 merged: CI run 35441393584, job
`linux_full::py3.10-s4`, `tests/test_automatic_writer_integration.py::test_concurrent_identical_append_converges_once_during_distinct_event_churn`
— `ValueError: transaction target changed while hashing: .../knowledge/daily/mixed-stress.md`.

Files: `scripts/markdown_transaction.py`,
`tests/test_an_append_whose_file_is_gone_is_written_again.py`,
`docs/research/2026-09-23-a-presence-check-does-not-hash.md`.

## What was found

- `_committed_append_or_rewrite` (added 2026-09-18, see
  `docs/research/2026-09-18-a-committed-append-still-needs-its-file.md`) has to answer one
  question: does the file the committed append wrote still exist. It asks it through
  `coordinator._current_hash(relative)`, which opens the file, hashes every byte and then
  refuses with `transaction target changed while hashing` whenever the file's size or mtime
  moved between the open and the close.
- That refusal is the right answer for a hash — a digest of a moving file is not evidence of
  anything — and the wrong answer for presence. The classification runs outside any lock, on a
  daily log that twelve other writers are appending to in the same second. The 2026-09-18 note
  said this itself: "bytes that merely changed are not checked: a daily log grows under every
  other writer, and that is not this block's disappearance". The code checked them anyway,
  because the only presence primitive it reached for was the hashing one.
- The failure is a race, so it passed the branch's CI and the local full suite and appeared once
  the merge commit ran the same test on a slower runner. Six of the eighteen workers in that
  test repeat one operation id; the sixth to settle hashes a file the other twelve are growing.
- The error escapes: `_append_value_failure` handles `ValueError` only on the prepare path
  (`OperationBoundElsewhereError`, `PreconditionChangedError`) and re-raises everything else,
  and `_settle_append_candidate` catches no `ValueError` at all. So one contended hash turns
  into a failed capture for a caller whose line is already on disk.

## Practice on this date

- POSIX separates the two questions by design: `lstat(2)` answers whether a name is bound to a
  file, atomically, without opening it; reading the bytes is a different call with different
  guarantees ([lstat(2), Linux man-pages](https://man7.org/linux/man-pages/man2/lstat.2.html)).
  A check that opens and reads when it only needs the name is exposed to every writer of the
  bytes for no gain.
- The module already holds the primitive: `_lstat_or_none` is what `_hash_bounded_target`
  itself calls first to decide `ABSENT`. The fix is to stop after that call.

## The decision

- `MarkdownCoordinator._target_present(path)` answers presence with `_lstat_or_none` on the
  resolved target and nothing else; `_committed_append_or_rewrite` uses it. A file that exists
  and is being written to is present; a hash of it is never taken on this path.
- A test pins the class: with the hashing primitive made to raise "changed while hashing", a
  repeated append of the same block under the same operation id is still answered with the
  committed record while the file exists, and still written again once the file is gone.
- Nothing else changes: the mutate path keeps `_verify_committed_targets` and its drift check,
  because there the after-image is the claim.

## Cost, by rule 4

One `lstat` instead of an open, a full read and two `fstat`s per repeated append; the common
case of the daily header becomes cheaper, not dearer.

## Sources

- [lstat(2) — Linux manual page](https://man7.org/linux/man-pages/man2/lstat.2.html) — fetched 2026-09-23.
- CI run 35441393584 on `main`, job `linux_full::py3.10-s4`, 2026-09-19.
- `docs/research/2026-09-18-a-committed-append-still-needs-its-file.md`.
