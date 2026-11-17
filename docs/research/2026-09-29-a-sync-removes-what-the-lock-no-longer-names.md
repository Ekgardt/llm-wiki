# A sync removes what the lock no longer names

Date: 2026-09-29. Status: implemented in the same change.

## What was found (live vault, read-only)

- The installed vault's `.venv` held 2.7 GB of `nvidia-*` CUDA wheels, installed
  2026-08-21 by the PyPI torch. `uv.lock` names none of them since torch comes from
  the explicit PyTorch CPU index on Linux
  (`docs/research/2026-09-29-every-install-brings-every-component.md`).
- Both syncs that keep an installed vault current were inexact:
  `installer_config.uv_sync_arguments` appended `--inexact` for an existing
  environment, and `self_update.BASELINE_SYNC_COMMAND` carried it. An inexact sync
  never removes, so a package the lock drops stays forever.
- A dry run of the exact sync this change produces for the live vault
  (`uv sync --locked --no-default-groups --dry-run --extra code-graph --extra full
  --extra retrieval-benchmark --extra semantic --group dev`): "Would uninstall 28
  packages", nothing to install — the 15 `nvidia-*` wheels, `cuda-bindings`,
  `cuda-pathfinder`, `cuda-toolkit`, `triton`, and `lancedb` with its
  dependencies (`lance-namespace*`, `pyarrow`, `deprecation`, `python-dateutil`,
  `six`, `urllib3`, `optimum`), which no code imports (LanceDB was retired on
  2026-09-07).

## Sources, read today

- uv, "Locking and syncing" (https://docs.astral.sh/uv/concepts/projects/sync/):
  "`uv sync` performs 'exact' syncing by default, which means it will remove any
  packages that are not present in the lockfile"; `--inexact` retains them; extras
  are synced only when named.
- uv, "Managing dependencies" (https://docs.astral.sh/uv/concepts/projects/dependencies/):
  groups are included with `--group`; `--no-default-groups` turns off the default
  `dev` group.
- astral-sh/uv#11937 (closed): an exact sync removes extras not requested again;
  the two ways to keep them are `--inexact` or naming them every time.
- `docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md`: inexact was
  chosen because the update then named no extra, and an exact sync would have
  uninstalled 93 packages. `docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`:
  the update learned to name the chosen extras, found from the environment.

## Decision

Name everything, then sync exactly. One selection for the installer and the nightly
update (`self_update.sync_selection`): the default extras, the extras the operator
installed (`chosen_extras`), and the dependency groups the operator installed
(`chosen_groups`: a group is chosen when a distribution only it brings is installed
and nothing else pulled it in — `pytest` means `dev`). The update syncs
`uv sync --locked --no-default-groups ... --extra ... --group ...` without
`--inexact`. The installer runs under the system Python, which on 3.10 may lack the
TOML reader, and only the environment's interpreter sees what is installed there,
so it asks that interpreter (`self_update.py --selection ROOT`, JSON). When the
environment cannot answer, the installer keeps `--inexact` and says so: it removes
nothing it cannot account for.

Kept inexact on purpose: `sync_memory`'s baseline step (it names no extra, so an
exact sync there would strip them) and every manual hint (`README*`, doctor,
`install_models`): a hint the operator types for one extra must not remove the rest
(`tests/test_the_guide_matches_the_passes.py`).

## Trade-offs

- An operator's own `pip install` into the managed environment, of something the
  lock does not name, is removed at the next update. The environment is the
  product's; anything else belongs in another environment.
- The first update after this change removes the 28 packages above from the live
  vault; the dry run shows nothing it would install or any package the code imports.

Guard: `tests/test_a_sync_removes_what_the_lock_no_longer_names.py` (a real virtual
environment with `pytest` installed: the installer's sync is exact and names `full`
and `dev`).
