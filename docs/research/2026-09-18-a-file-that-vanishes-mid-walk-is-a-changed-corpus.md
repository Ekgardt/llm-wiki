# A file that vanishes mid-walk is a changed corpus

Dated 2026-09-18 (the work began on 2026-09-17 and the clock rolled over). Third audit,
second round: the item "the file deleted between discovery and read that still surfaces as
*exceeds bounds*". The research before the fix.

Files: `scripts/corpus_snapshot.py`,
`tests/test_a_file_that_vanishes_mid_walk_is_a_changed_corpus.py`.

## What was found

- The corpus walk lists a directory and then opens each entry it listed: on POSIX with
  `os.open(name, …, dir_fd=…)`, on Windows with `entry.stat(follow_symlinks=False)`. A file
  deleted between the two raises `FileNotFoundError`, and nothing on either path turns it
  into the module's own `CorpusChanged`.
- Everything else the walk can notice about a moving tree is already `CorpusChanged`: a
  directory descriptor whose identity changed, a child directory swapped before the open, a
  source whose identity changed before its descriptor, a sealed source that vanished
  (`_require_source_as_sealed`). Only the listing-to-open window was left out.
- Two consequences. `collect_corpus` retries a `CorpusChanged` pass and only gives up after
  several ("corpus never held still for one pass"); a `FileNotFoundError` skipped that retry
  and ended the capture at once. And `repository_index._collect` maps every `OSError` to the
  refusal `repository_exceeds_corpus_bounds` — "the corpus collector refused this
  repository" — so a deleted temporary file told the operator their repository is too big.

## Practice on this date

- `open(2)`: "ENOENT — O_CREAT is not set and the named file does not exist"
  (https://man7.org/linux/man-pages/man2/open.2.html, fetched 2026-09-17). A listing is a
  snapshot of names, not a lease on them: between `readdir` and `openat` any name may be
  gone, and a walk that reads a live tree must expect it.
- The module's own rule, stated at `_seal_source_file`: "The file itself moving is somebody
  writing it, which the descriptor walk calls `CorpusChanged`, so this one does too."

## The decision

One helper opens a listed entry (`_opened_listed_entry`) and one reads a listed entry's
metadata (`_Discovery._entry_info`); both call a vanished name `CorpusChanged`. The capture
then retries as it does for every other kind of movement, and a repository that keeps
changing is reported as being written, not as too large.
