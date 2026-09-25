# The installers agree

Date: 2026-09-25. Audit item C-34 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- (a) `install.ps1` does not swallow a failed install transaction: it sets `$schedulerWarning`,
  finishes the remaining steps and exits 1 at the end; `install.sh` stops at once. Different
  timing, same outcome. No change.
- (b) `install.sh` reads the `llm-wiki` entry in `~/.claude.json` and says when it points at
  another vault; `install.ps1` only matched the name, so another vault's entry counted as this
  install's.
- (c) `_uninstall_launchd` required `launchctl bootout` to succeed for every job; a job launchd no
  longer has makes `bootout` fail, which blocked the whole uninstall.
- (d) The installer never enables systemd lingering, and the guide did not say so.

## Source

- loginctl(1), https://man7.org/linux/man-pages/man1/loginctl.1.html (fetched 2026-09-25; the
  freedesktop.org copy answered 403): "If enabled for a specific user, a user manager is spawned
  for the user at boot and kept around after logouts. This allows users who are not logged in to
  run long-running services."

## Decision

- (b) `install.ps1` gains `Get-ClaudeMcpState` (missing / absent / current / elsewhere, the file
  only read) and names another vault's entry with the commands to replace it, as `install.sh`
  does; its status line comes from a guard-clause function.
- (c) The launchd teardown boots out only jobs `launchctl print` reports as loaded.
- (d) Lingering stays off: the product claims no logged-out execution on any platform, and turning
  it on changes the operator's session policy. The guide now says so and names the command.
- A test fails if the launchd teardown requires an unloaded job to boot out, or if `install.ps1`
  stops checking which vault the MCP entry names.

## Files

- `install.ps1`
- `scripts/install_control.py`
- `docs/USER-GUIDE.md`
- `tests/test_the_installers_agree.py`
- `CHANGELOG.md`
