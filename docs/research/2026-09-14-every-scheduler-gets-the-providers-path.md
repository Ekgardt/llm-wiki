# Every scheduler gets the provider's PATH

Dated 2026-09-14. Item 4.1 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- On Linux the nightly compile could not find any provider on 2026-08-22: the systemd
  user unit inherited the manager's PATH, which holds no per-user directory, while the
  provider CLI lived in `~/.local/bin` beside `uv`. Commit `895d04a` gave the unit
  `PATH=<uv's directory>:<the manager's PATH>` (`install_control._scheduled_path`) and it
  also carries the provider variables (`_provider_items`: `MEMORY_LLM_PROVIDER`,
  `MEMORY_CLAUDE_MODEL`, from `integration_hook_config.PROVIDER_ENV_KEYS`; no secrets).
- `install_control._launchd_job` (macOS LaunchAgent) sets `LLM_WIKI_ROOT`,
  `LLM_WIKI_STATE_ROOT` and the provider variables, but no `PATH`.
- `installer_config.build_cron_command` (the explicit cron fallback, rendered by
  `install_control.render_cron_block` and printed by `installer_config cron-command`)
  sets `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT` only: no `PATH`, no provider variables.
- So on macOS and under cron the scheduled compile resolves providers against the
  scheduler's default PATH — the same failure Linux had. Not observed here (this
  machine runs systemd); stated from the code and the documented defaults below.
- The code graph: `_launchd_job` ← `render_launchd_definitions` ← the macOS install
  path; `build_cron_command` ← `render_cron_block`, `_cron_command`. Tests pin the
  launchd environment exactly (`tests/test_install_control.py`) and check the cron
  command with `sh -n` (`tests/test_installer_config.py`).

## Practice on this date

- launchd gives jobs a minimal PATH, `/usr/bin:/bin:/usr/sbin:/sbin`, unless the job's
  `EnvironmentVariables` set one
  ([launchd.plist(5), EnvironmentVariables](https://keith.github.io/xcode-man-pages/launchd.plist.5.html);
  `launchctl config user path` documents the same default).
- cron runs commands with `PATH=/usr/bin:/bin` unless the crontab sets it
  ([crontab(5), cronie](https://man7.org/linux/man-pages/man5/crontab.5.html)).
- The rule this codebase already chose for systemd: prepend the directory of the `uv`
  the job calls by absolute path, keep the scheduler's default after it.

## The decision

- One helper, `installer_config.scheduled_path(uv_path, default_path)`, returns
  `<uv's directory>:<default_path>`; systemd keeps its value, launchd gets
  `/usr/bin:/bin:/usr/sbin:/sbin` after the uv directory, cron `/usr/bin:/bin`.
- The launchd job's `EnvironmentVariables` gain `PATH`. The cron command gains `PATH`
  and the same provider variables the other schedulers carry, each shell-quoted.
- Existing macOS or cron installs see their definitions change and are re-rendered by
  the next install, as Linux units were after `895d04a`.

Files: `scripts/installer_config.py`, `scripts/install_control.py`,
`tests/test_install_control.py`, `tests/test_every_scheduler_gets_the_providers_path.py`,
`docs/research/2026-09-14-every-scheduler-gets-the-providers-path.md`.
