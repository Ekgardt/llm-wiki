# An update that keeps what is installed, and says why it could not fetch

Dated 2026-09-14. Items 1.1 and 4.2 of `docs/AUDIT-2026-09-14-2.md`. The research
before the fix.

## What was found

- After a successful fast-forward, `self_update._synced_dependencies` runs
  `uv sync --locked --no-dev`. Dry-run of that exact command on the live `.venv`
  (`--dry-run --offline`, 2026-09-14): **"Would uninstall 93 packages"** — torch,
  transformers, sentence-transformers, onnxruntime, numpy, every tree-sitter
  package. The first night that actually pulls a commit would strip semantic
  search, the reranker and the code graph. It has not happened: no nightly log says
  `updated`. `tests/test_self_update.py` replaces the function with a stub, so the
  command was never exercised.
- Every other place that syncs this project passes `--inexact`:
  `sync_memory._SYNC_STEP` (`uv sync --locked --inexact --no-default-groups
  --no-python-downloads --quiet`), `installer_config` for an existing environment.
  Dry-run of the `sync_memory` command on the same `.venv`: **"Would make no changes"**.
- `self_update._fetched` returns only a boolean; git's error text is thrown away and
  the nightly logs `skipped (fetch_failed)`. The fetch failed on 2026-09-14 and
  2026-09-01 and nobody can say why.

## Practice on this date

- uv: `uv sync` performs exact syncing by default and removes any package not
  present in the lockfile selection; `--inexact` retains extraneous packages, and
  extras installed earlier but not requested again are removed by an exact sync
  ([uv: locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/),
  [keeping extras when running `uv sync`, astral-sh/uv#11937](https://github.com/astral-sh/uv/issues/11937)).
- An operator-facing refusal names its cause (this codebase's own rule, audit OPS-09,
  `secret_redact.describe_error`).

## The decision

1. `self_update` syncs with the project's baseline command —
   `uv sync --locked --inexact --no-default-groups --no-python-downloads --quiet` —
   as a named constant, and a test pins it equal to `sync_memory._SYNC_STEP.command`
   so the two cannot drift again (importing `sync_memory` here would pull in
   `doctor`).
2. A failed fetch returns `skipped`, `fetch_failed`, and `detail`: git's stderr,
   redacted and cut to its last 300 characters; the nightly log prints it.

Files: `scripts/self_update.py`, `scripts/scheduled_nightly.py`, `tests/test_an_update_keeps_what_is_installed.py`,
`docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md`.
