# A revision walks no ignored top-level folder

Date: 2026-09-25. Audit item A-16 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `workspace_revision._relevant_files` walks the whole checkout, pruning only hidden and
  `__pycache__` directories. The audit measured a TypeScript checkout with its dependencies
  installed: 14.8 s in `node_modules`, then a refusal at `MAX_REVISION_FILES` (100 000), so code
  navigation failed for the repository.
- The corpus it proves fresh takes only tracked top-level entries as code roots
  (`repository_index.tracked_top_level_entries`), so a top-level folder git ignores whole is never
  indexed; walking it proved nothing.
- Nested ignored folders under a tracked root are walked by both the corpus and the revision
  (consistent); this change does not alter that.

## Source

- git-ls-files, https://git-scm.com/docs/git-ls-files (fetched 2026-09-25): `--directory` "If a
  whole directory is classified as "other", show just its name (with a trailing slash) and not its
  whole contents."; `--ignored` "Show only ignored files in the output."; `--exclude-standard` "Add
  the standard Git exclusions: .git/info/exclude, .gitignore in each directory, and the user's
  global exclusion file."

## Decision

- Both revision walks (compute and verify) skip the top-level directories that
  `git ls-files --others --ignored --exclude-standard --directory -z` names; when git cannot answer,
  none is skipped. The link rule is unchanged; its test now puts the escaping link in a tracked tree.

## Uncertainty

- A monorepo with `node_modules` inside a tracked package still walks it (as the corpus does) and
  can still reach the ceiling. Pruning ignored folders inside roots would have to change the
  corpus walk too, which also reads the vault's private (ignored) memory tree, and is not done here.

## Files

- `scripts/workspace_revision.py`
- `tests/test_a_revision_walks_no_ignored_top_level_folder.py`
- `tests/test_a_directory_link_inside_the_checkout_does_not_refuse_the_revision.py`
- `CHANGELOG.md`
