# launchd: absent only when launchd says so

Date: 2026-09-26. Audit 2026-09-26, regression list item 11 (a suspicion, now
confirmed in the code).

## What was wrong

`install_control._launchd_job_state` read every non-zero exit of
`launchctl print <domain>/<label>` as "absent". Uninstall skips `bootout` for an
absent job, so a `print` that failed for another reason (a domain it could not
reach, a transient launchd error) left the job loaded while its plist was removed,
and the scheduler projection reported "absent" instead of a conflict.

## Decision

- Exit 0 is `active`, exit 113 is `absent`, anything else is `unknown`
  (`LAUNCHD_SERVICE_NOT_FOUND = 113`).
- `_bootout_if_loaded` skips only an `absent` job. An `unknown` one gets its
  `bootout`; if that fails too, the uninstall stops and says so instead of leaving
  a loaded job behind.
- `unknown` is neither `absent` nor `active`, so `_combined_scheduler_state`
  reports `conflict` for it, as it already does for any mixed state.

Not verified on a Mac here (Linux machine): the code paths are covered with a fake
`launchctl` runner, as the existing installer tests do.

## Sources

- ss64, launchctl, https://ss64.com/mac/launchctl.html, fetched 2026-09-26:
  "launchctl will exit with status 0 if the subcommand succeeded. Otherwise, it
  will exit with an error code that can be given to the error subcommand to be
  decoded into human-readable form." — so a non-zero exit is an error code, not
  one fixed meaning.
- cregis-dev/apex PR #57, https://github.com/cregis-dev/apex/pull/57, fetched
  2026-09-26: "On a label that is installed but not loaded, launchctl exits 113
  with: Bad request. Could not find service "dev.cregis.apex" in domain for user
  gui: 501".
- Alan Siu, https://www.alansiu.net/2025/05/28/using-new-launchctl-subcommands-to-check-for-and-reload-launch-daemons/,
  fetched 2026-09-26: an unloaded label prints "Bad request. Could not find
  service … in domain for system".

## Files

- `scripts/install_control.py`
- `tests/test_launchd_absent_only_when_launchd_says_so.py`
