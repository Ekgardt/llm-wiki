# Every git call has a deadline

Date: 2026-09-26. Audit 2026-09-26, finding C-13 (`snapshot_knowledge` calls git
without a timeout and with the operator's global config).

## What was wrong

Measured on 2026-09-26 by walking the syntax tree of `scripts/`: six calls start
`git` through `subprocess.run`/`check_output` with no `timeout`:
`snapshot_knowledge._git`, `memory_state._resolve_vault_root` (run at import by
every hook), `lint_memory._git_tracked_paths`, `release_manifest.commit_of` and
`_blob`, `release_tag._git`. A git that waits — a stale lock, a signing agent
asking for a passphrase — held the caller without end. The snapshot also committed
under the operator's global and system config, so a global `commit.gpgsign`,
`core.hooksPath` or `core.fsmonitor` made the copy's commits sign, run hooks or
start a monitor; `git init`/`git config` failures there were ignored.

`impact_analysis._start_git` uses `Popen` and is bounded by its caller's
deadline elsewhere; it belongs to the navigation work and is not changed here.

## Decision

- Each call gets a timeout named for what it does: 2 s for the hook-time
  `rev-parse` probe, 30 s for `ls-files` in lint, 60 s for the release tools,
  120 s for the snapshot's add/commit of a 20 MB tree. A timeout is handled where
  the call's other failures are: the probe and lint fall back as before, the
  snapshot raises `SnapshotFailed` naming the git step.
- The snapshot runs git with `GIT_CONFIG_GLOBAL=/dev/null` and
  `GIT_CONFIG_NOSYSTEM=1`; its own repository config (`user.name`, `user.email`)
  still applies. `init` and `config` failures now stop the snapshot.
- Guard: `tests/test_every_git_call_has_a_deadline.py` walks every script and fails
  on any waiting subprocess call whose command starts with `"git"` and has no
  `timeout`, so a new one cannot be added unnoticed.

## Sources

- Git documentation, git-config, https://git-scm.com/docs/git-config, fetched
  2026-09-26: `GIT_CONFIG_GLOBAL` — "Take the configuration from the given files
  instead from global or system-level configuration." `GIT_CONFIG_NOSYSTEM` —
  "Whether to skip reading settings from the system-wide $(prefix)/etc/gitconfig
  file."
- Python documentation, subprocess, https://docs.python.org/3/library/subprocess.html,
  fetched 2026-09-26: "If the timeout expires, the child process will be killed
  and waited for. The `TimeoutExpired` exception will be re-raised after the child
  process has terminated."

## Files

- `scripts/snapshot_knowledge.py`
- `scripts/memory_state.py`
- `scripts/lint_memory.py`
- `scripts/release_manifest.py`
- `scripts/release_tag.py`
- `tests/test_every_git_call_has_a_deadline.py`
