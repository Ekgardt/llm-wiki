# A gone top-level folder does not stop the index

Date: 2026-09-25. Audit item A-15 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (reproduced by test)

- `repository_index._detected` and `_rebuilt` took the code roots recorded by the first index
  (`policy.code_roots`) and passed them back as an explicit request. After a top-level folder was
  deleted or renamed, `detect`, `refresh` and the nightly `refresh-all` refused with the
  collector's refusal for that repository, every night (old code: `RepositoryIndexRefused`).
- A new top-level folder was never indexed, and `changes` did not say so.
- The recorded policy does not say whether the roots were discovered or requested, so a refresh
  cannot know whether a new folder was deliberately left out.

## Source

- git-ls-files, https://git-scm.com/docs/git-ls-files (fetched 2026-09-25): "This command merges
  the file listing in the index with the actual working directory list, and shows different
  combinations of the two." The tracked top-level entries (`tracked_top_level_entries`) are read
  this way, so the set of entries an index could cover is always current.

## Decision

- A refresh uses the recorded roots that still exist (`_live_roots`); a removed root's files are
  reported as removed and rebuilt away. When none is left, the roots are discovered again.
- `detect` (and so `changes`) names `uncovered_roots`: tracked top-level entries this index does
  not cover, the memory tree aside. They are not added on their own, because the policy cannot
  say whether they were left out on purpose; indexing with `roots=` includes them.

## Files

- `scripts/repository_index.py`
- `tests/test_a_gone_top_level_folder_does_not_stop_the_index.py`
- `CHANGELOG.md`
