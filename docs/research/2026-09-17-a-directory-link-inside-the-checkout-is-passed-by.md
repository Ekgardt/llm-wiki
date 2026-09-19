# A directory link that stays inside the checkout is passed by

Dated 2026-09-17. Third audit, finding G-H3. The research before the fix.

Files: `scripts/workspace_revision.py`,
`tests/test_a_directory_link_inside_the_checkout_does_not_refuse_the_revision.py`.

## What was found

- `workspace_revision._refuse_unsafe_entry` raises `PermissionError` for every symlinked or
  reparse-point directory the walk meets, wherever it points. The walk skips only `.git` and
  does not read `.gitignore`.
- Every `venv`/`uv` environment on Linux holds `lib64 -> lib`. So any checkout with a
  `.venv` fails `compute_workspace_revision`, and `code_navigation` answers
  `revision computation failed`: precise navigation is refused in the checkouts where it is
  wanted most.
- Reproduced on a temp repository (`pkg/a.py`, ignored `.venv/lib`, `.venv/lib64 -> lib`):
  `PermissionError: workspace revision relevant path is a symlink or reparse directory`.
- What the refusal protects: the revision must cover every file the language server may
  read for this workspace, and the walk must never be led outside the checkout. A link that
  leaves the root breaks both. A link whose target is a directory inside the root breaks
  neither: the walk never follows it, and the target's files are hashed at their real path
  by the same walk.
- Existing pins stay true: `test_non_git_manifest_rejects_relevant_symlinks` refuses a link
  to a directory outside the root, a linked `.py` and a linked `pyrightconfig.json`.

## Practice on this date

- Pyright's own default leaves such trees out of the project: "By default Pyright also
  excludes the following: `**/node_modules`, `**/__pycache__`, `**/.*` (hidden
  directories)", while "files in the exclude paths may still be included in the analysis if
  they are referenced (imported) by source files that are not excluded"
  (https://github.com/microsoft/pyright/blob/main/docs/configuration.md, fetched
  2026-09-17). So the environment's files still matter to the answer — the walk is right to
  hash them — and they are reached at their real path.
- The containment rule used elsewhere in this module is resolve-then-compare against the
  resolved root (`_directory_snapshot_for`: `snapshot.resolved.relative_to(resolved_root)`).
  The same test decides a link.

## The decision

- A directory link (symlink or reparse point) with a name that is not itself relevant, whose
  strictly resolved target lies inside the resolved checkout root, is passed by: not
  followed, not hashed, not refused. Everything else keeps today's behaviour: a
  directory link that leaves the root or cannot be resolved is refused, any link with a
  relevant name is refused, and a link to a file that is not relevant is skipped as before.
- Known limit, stated rather than hidden: the link's own target text is not part of the
  revision, so re-pointing such a link from one in-root directory to another is not seen as a
  change. Every file either target holds is still hashed.
- Not decided here (owner's call, listed in the report): whether the walk should honour
  `.gitignore` so an environment's ~10 000 files stop counting against
  `MAX_REVISION_FILES`.
