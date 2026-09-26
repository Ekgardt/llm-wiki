# An untracked copy of what the update adds does not stop it

Date: 2026-09-26. Audit 2026-09-26 A-9.

## Facts

- The live vault holds this project's research notes as untracked files under
  `docs/research/`: the rule-2 gate reads them from the main checkout, so every
  change is researched there first and the same bytes reach `main` through a pull
  request.
- `scripts/self_update.py` fast-forwards with `git merge --ff-only`. Git refuses a
  merge that would overwrite an untracked file. git-read-tree documentation
  (https://git-scm.com/docs/git-read-tree, fetched 2026-09-26): "if you have local
  changes in the working tree that would be overwritten by this merge, git
  read-tree will refuse to run to prevent your changes from being lost"; only
  `--reset -u` overrides it, and then "updates leading to loss of working tree
  changes or untracked files or directories will not abort the operation".
- `_git` raised `SelfUpdateError("git merge failed")` and dropped git's stderr, so
  the nightly log named neither the file nor the cause.

## Decision

- Before the merge, the update lists the paths it adds
  (`git diff --name-only -z --diff-filter=A head..fetched`) that exist in the
  working tree. For each, the file's blob id (`git hash-object`) is compared with
  the fetched tree's (`git ls-tree -z fetched`).
- Every such file identical to what the update brings is moved aside and the
  merge recreates it byte for byte; nothing is lost. If the merge then fails, the
  moved files are put back. A symbolic link or a file whose content differs stops
  the update as `untracked_files_conflict`, naming at most 20 paths — the owner's
  bytes are never overwritten (`--reset` is not used).
- `_git` failures carry git's own words, redacted and bounded, as the fetch
  failure already did.

## Files

- `scripts/self_update.py`
- `tests/test_an_untracked_copy_of_the_update_does_not_stop_it.py`
- `CHANGELOG.md`
