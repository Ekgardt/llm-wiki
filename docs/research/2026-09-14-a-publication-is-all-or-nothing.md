# A restored image is published all or nothing

Dated 2026-09-14. Item 2.9 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `private_vault_backup.publish_restored_image` puts a validated restore image into an
  installed vault. It walks the image's files and publishes each with
  `_publish_one`: an exclusive create (`open(..., "xb")`), where an identical existing
  file counts as "identical" and a different one raises `publish_conflict`.
- The conflict is found only when that file's turn comes. Every file before it is
  already written, so a refusal of a populated vault leaves a half-merged vault — the
  outcome the docstring says it prevents ("refused by the first conflicting path rather
  than merged"). The existing test only checks the conflicting file itself.
- Nothing is flushed to disk: no `fsync` of the files or their directories, so a crash
  soon after a "successful" publication can lose files that the receipt counted.
- The code graph: `publish_restored_image` ← `_run_cli_command` ← `main` (the
  `publish` subcommand). Tests: `tests/test_private_vault_backup.py` (empty vault,
  repeat, conflict, tampered image).

## Practice on this date

- Check every precondition before the first irreversible step, and make the irreversible
  steps undoable until the operation is complete (two-phase: plan, then apply with
  compensation) — the same shape as this codebase's Markdown transactions.
- A file is durable after `fsync` of the file and of the directory entry that names it
  ([fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html), "Calling fsync() does
  not necessarily ensure that the entry in the directory containing the file has also
  reached disk"); `reliable_memory.fsync_directory` is this codebase's helper.

## The decision

- Plan first: every destination is checked before anything is written; any destination
  that exists with different bytes refuses the whole publication with
  `publish_conflict`, naming the first such path, and nothing is written.
- Apply: each file is created exclusively, written, `fsync`ed, and its directory
  `fsync`ed. If anything fails part-way (a file that appeared after the plan, a disk
  error, the deadline), every file this run created is removed again before the error
  is raised. Files that were already identical are never touched.

Files: `scripts/private_vault_backup.py`, `tests/test_a_publication_is_all_or_nothing.py`,
`docs/research/2026-09-14-a-publication-is-all-or-nothing.md`.
