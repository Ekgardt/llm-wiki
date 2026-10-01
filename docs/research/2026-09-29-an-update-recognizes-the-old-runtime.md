# Identify chosen extras before replacing their dependency declarations

2026-09-29, uv 0.12.3, baseline a5c67aee.

The installed semantic runtime lacked onnxruntime after moving from the old
sentence-transformers encoder. `_merged_update` first replaced pyproject.toml,
then inferred selected extras from the new declarations and old environment.
A real local Git update reproduces the loss: an installed old semantic runtime
no longer identifies semantic, so the sync receives no extra and installs no
replacement. The regression asserts the actual sync selection across the merge.

Primary sources checked today:

- [uv sync](https://docs.astral.sh/uv/concepts/projects/sync/): optional groups
  are not selected by default; inexact sync retains extra installed packages
  but does not select their replacement dependencies.
- [Packaging specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/):
  optional dependency declarations define extras in a particular project version.
- [Python importlib.metadata](https://docs.python.org/3/library/importlib.metadata.html):
  installed distribution metadata describes the actual installed environment.

Keep the existing inference and sync contracts, but infer choices before the
fast-forward. Then resolve those choices against the new lock. No new state file
or guessed package mapping is needed. Rejected: inferring solely after merge,
always installing every optional dependency, or hardcoding the encoder migration.
This applies to any supported extra whose implementation changes. Renaming or
removing an extra remains an explicit migration, not a silently guessed choice.

The currently broken installation required an explicit semantic sync and the
normal pinned-model installer; the code change prevents recurrence on normal
updates and does not retroactively establish the cause of every manual install.
