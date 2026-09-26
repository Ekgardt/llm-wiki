# A busy checkout is changed during its own read

Date: 2026-09-26. CI on PR 43, commit 36ad4a45, macOS job.

## Fact
- `test_a_checkout_being_written_is_refused_by_name_and_the_others_are_refreshed`
  ran a writer thread in a loop and expected the refresh of that checkout to be
  refused with `repository_changed_during_capture`. On macOS the capture read the
  42 small files without the writer getting a turn, found a consistent tree and
  refreshed it: `('refreshed', None)`. Whether a write lands inside the capture
  was left to thread scheduling.
- The product's check is exact: `corpus_snapshot._read_bounded_descriptor` stats
  the descriptor before and after `_read_chunks` and raises `CorpusChanged` when
  the file changed in between.

## Source (fetched 2026-09-26)
Python glossary, "global interpreter lock", https://docs.python.org/3/glossary.html:
"The mechanism used by the CPython interpreter to assure that only one thread
executes Python bytecode at a time." A Python writer thread runs only when the
reading thread yields, so it cannot be relied on to write inside a short read.

## Decision
The test changes a busy file exactly while the capture reads it: `_read_chunks`
is wrapped so that, for a file of the busy checkout, the file is appended to
before its bytes are read. The real before/after `fstat` check then refuses the
checkout, every run, on every platform. The quiet checkout is still refreshed in
the same pass.

## Follow-up (CI on 3d756cd6, Windows py3.10 and py3.12)
The busy checkout came back `('fresh', 'unchanged')`: nothing in it had changed
since it was indexed, so the refresh never read it and the injected write never
ran. The test now adds a file to the busy checkout before the refresh, as it
already does for the quiet one, so the refresh must capture it on every
platform.

## Files
- tests/test_one_busy_or_broken_checkout_does_not_end_the_pass.py

## Follow-up (2026-09-26): Windows reads by path

Fact: CI run 36209780525 failed the test on every Windows job — `busy` was
`refreshed`. `corpus_snapshot._Discovery.add` reads through a descriptor walk only
on POSIX; on Windows the content is read later by `_sealed_source_bytes`, which
calls `bounded_io.read_stable_bytes`, and never reaches `_read_chunks`. So the
test's write never happened there.

Decision: `tests/test_one_busy_or_broken_checkout_does_not_end_the_pass.py` places
the write inside whichever read the platform performs — `_read_chunks` on POSIX,
`bounded_io._read_open_descriptor` on Windows, whose identity check (device,
inode, size, mtime) then raises `SourceChangedDuringRead`, mapped to
`CorpusChanged`. Checked on Linux by calling `_sealed_source_bytes` with the
Windows-side patch: it is refused with "corpus source changed during read".
The product code is unchanged.
