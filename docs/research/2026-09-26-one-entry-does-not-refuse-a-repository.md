# One entry does not refuse a repository

Date: 2026-09-26. Audit 2026-09-26 A-8 (and nav findings 5 and 16 of the same audit).

## Facts

- Under a code root the corpus walk raised on the first odd entry, and
  `repository_index._collect` turned every `OSError`/`ValueError` into
  `repository_exceeds_corpus_bounds`: one symbolic link (`_require_safe_entry`), one
  file past `MAX_CORPUS_FILE_BYTES` (8 MiB, `_read_bounded_descriptor`), one name
  that is not UTF-8, or a nested `web/node_modules` (its `.bin/` links) made the
  whole repository unindexable, with a reason that named none of them.
  `tests/test_one_entry_does_not_refuse_a_repository.py` reproduces it on the old
  code (both tests fail).
- The collector already leaves a binary file out ("One stray binary under a code
  root must not be able to fail the whole generation") and an unnameable path.
- Git ignores were read only at the top level
  (`workspace_revision.ignored_top_level_directories`). git-ls-files documentation
  (https://git-scm.com/docs/git-ls-files, fetched 2026-09-26): `--directory` — "If a
  whole directory is classified as "other", show just its name (with a trailing
  slash) and not its whole contents"; `--exclude-standard` — ".gitignore in each
  directory". So the one listing already names ignored directories at any depth.
- On the live vault checkout that listing includes `knowledge/projects/<slug>/`.
  The revision walk counts only navigable code suffixes and configuration names
  (`_is_relevant_path`), and the memory corpus does not take the pruned set, so
  project state is not affected.

## Decision

- Under a code root only, a link, a file past the size bound, and a name that is
  not UTF-8 are skipped and named (`CorpusSnapshot.skipped`); the index receipt
  reports `skipped_entries` and the first twenty `skipped_examples`. Knowledge,
  project and daily walks keep refusing such entries.
- `workspace_revision.ignored_directories` returns every directory git ignores,
  at any depth; the revision walk prunes by root-relative path, and
  `repository_index` passes the same set to `collect_corpus(pruned_directories=)`.
  The set is not snapshot policy, so no stored corpus hash changes.

## Files

- `scripts/corpus_snapshot.py`
- `scripts/workspace_revision.py`
- `scripts/repository_index.py`
- `tests/test_one_entry_does_not_refuse_a_repository.py`
- `tests/test_a_revision_walks_no_ignored_top_level_folder.py`
- `tests/test_workspace_revision.py`
- `CHANGELOG.md`

## Follow-up (2026-09-26): a code file whose bytes are not UTF-8 (audit B-13)

Fact: `_Capture.add` leaves out a file that does not decode as UTF-8 (`_decodes_as_utf8`)
and said nothing, so a Latin-1 Python module vanished from the graph. Decision: under a
code root such a file is added to the same `skipped` list, so the receipt counts and
names it. Honest limit: the generation itself does not record the skip, so
`graph_complete` in a later answer still speaks only of unresolved observations; the
receipt of the build is where the omission is stated.
