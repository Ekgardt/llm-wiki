# A backup image carries what Git does not

Date: 2026-09-17. Trigger: third audit, Q-M16, second half — the owner's
question was "what should the backup image contain so that `publish` can land
in a real checkout". The owner delegated the decision; this note records it.

## What was found (reproduced)

`private_vault_backup` scans the vault root and excludes only the three
top-level runtime directories `cache/`, `logs/`, `run/`. Since the
single-directory decision of 2026-08-21 the vault root *is* the git checkout,
so the image also carries `.git/`, `.venv/`, `scripts/`, `tests/`, `docs/` —
the whole product, plus its virtual environment.

Two consequences:

1. `publish` refuses. It writes a file only where the destination is absent or
   byte-identical, and it refuses the whole publication if one file differs. A
   fresh clone differs in `.git/config`, in `.git/index`, in every `.venv`
   path. The audit reproduced `publish_conflict ('vault/.git/config',)` into a
   fresh clone — that is, the last step of moving memory to a new machine could
   not run at all.
2. The image is large and mostly redundant: `.git` and `.venv` on this machine
   are hundreds of megabytes, and every byte of them is either in the remote
   repository or reinstallable from `uv.lock`.

## Sources

1. Restic, *Backing up*: exclusion is the normal shape of a backup source —
   `--exclude` "Specify one or more times to exclude one or more items",
   `--exclude-file` "Specify one or more times to exclude items listed in a
   given file", `--exclude-caches` "Specify once to exclude a folder's content
   if it contains the special CACHEDIR.TAG file, but keep `CACHEDIR.TAG`",
   `--exclude-if-present foo` "Specify one or more times to exclude a folder's
   content if it contains a file called `foo`".
   https://restic.readthedocs.io/en/stable/040_backup.html (fetched 2026-09-17)
   Restic's own answer is a list of exclusions, not "back up everything";
   `--exclude-caches` exists precisely because a tree that can be regenerated
   does not belong in a snapshot.
2. This repository, `CLAUDE.md`: "Coherent encrypted private-vault recovery uses
   Restic with the existing maintenance fence, SQLite online backup, manifests,
   and staged validation" — the subject is the *private vault*. The same file:
   "`cache/` and `logs/` are disposable"; the product itself lives in git and is
   installed by `install.sh` / `install.ps1`.
3. This repository, `docs/research/2026-09-14-a-publication-is-all-or-nothing.md`
   and `publish_restored_image`: publication overwrites nothing, by design.

## Decision

The image carries **what Git does not already carry**: every file under the
vault root that git does not hold in exactly the state it has on disk, plus the
runtime `run/` half, exactly as before.

- One `git ls-files -z` and one `git diff --name-only -z HEAD` (both bounded by
  the run's deadline, both read-only) give the set of tracked paths whose
  content is identical to `HEAD`. Those paths are skipped.
- A tracked file the owner has **modified** is *not* skipped: uncommitted work
  is exactly what a backup must hold.
- `.git/` and `.venv/` join `cache/`, `logs/`, `run/` as excluded top-level
  names: a clone restores the first, `uv sync` the second, and neither is
  private memory. Alongside them go the tool caches `__pycache__`,
  `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, and `node_modules`, at any
  depth — regenerable by definition, and the same class `--exclude-caches`
  names.
- Where git cannot answer (no `git` binary, or a vault root that is not a
  repository) nothing is skipped beyond those names, so an installation without
  git keeps the old, complete image rather than a quietly thinner one.
- Directories that end up holding nothing are not part of the image, so the
  image is the shape of what it carries. `publish` already writes regular files
  only, and its receipt already counts what it leaves out.

This makes `publish` land in a fresh clone: every file in the image is either
absent there (the memory, the runtime, the owner's untracked files) or
byte-identical (a tracked file that was modified and has since been committed).

Not decided here, and unchanged: the `run/` half, the manifest, the validation,
the fence, and the refusal shape.

Files: `scripts/private_vault_backup.py`,
`tests/test_a_backup_image_carries_what_git_does_not.py`,
`docs/USER-GUIDE.md`, `docs/STRUCTURE.md`.
