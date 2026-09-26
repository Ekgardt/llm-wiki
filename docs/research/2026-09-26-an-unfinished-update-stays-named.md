# An unfinished update stays named

Date: 2026-09-26. Audit 2026-09-26 B-24.

## Facts

- `scheduled_nightly._update_code` replaced `last_update` every night. After an
  update that left `dependencies: stale` or `resources: rerun_installer`, the next
  night found nothing to fetch (`status: current`), and doctor's warning vanished
  while the dependencies were still unsynced and the installer still not rerun.
- Doctor advised "run `uv sync`". uv documentation
  (https://docs.astral.sh/uv/concepts/projects/sync/, fetched 2026-09-26): "uv sync
  performs "exact" syncing by default, which means it will remove any packages
  that are not present in the lockfile", and "To retain extraneous packages, use
  the `--inexact` flag". The advice would have removed every optional extra.
- The installer records its last commit as `committed_at` in
  `run/install/manifest.json` (read on the live vault, read-only).

## Decision

- On a night whose update is `current`: stale dependencies are synced again
  (`self_update.sync_dependencies`, the same inexact baseline-plus-chosen-extras
  sync the update runs) and the result recorded; `rerun_installer` is carried,
  with the moment it was first asked for (`resources_since`), until the install
  manifest's `committed_at` is later. Doubt reading the manifest keeps the warning.
- Doctor's advice says the nightly syncs again and names
  `uv sync --locked --inexact` with the operator's `--extra` flags.

## Files

- `scripts/scheduled_nightly.py`
- `scripts/self_update.py`
- `scripts/doctor.py`
- `tests/test_an_unfinished_update_stays_named.py`
- `CHANGELOG.md`
