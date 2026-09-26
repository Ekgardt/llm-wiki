# A scheduled run keeps the uv link

Date: 2026-09-25. Audit item B-30 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- Every scheduler definition (systemd, launchd, cron, Windows task spec) and the install
  manifests named uv as `Path(uv_path).resolve()`, nine places in `install_control.py`, and
  `installer_config.scheduled_path` put the resolved directory on PATH.
- The installer passes uv as the shell finds it (`command -v uv`); with Homebrew that is the
  link `/opt/homebrew/bin/uv`, whose target lies in a versioned `Cellar/uv/<version>/bin/`.
- Not reproduced on macOS here (no Mac); reproduced the path arithmetic with a link on Linux.

## Source

- Homebrew FAQ, https://docs.brew.sh/FAQ (fetched 2026-09-25): "Homebrew automatically
  uninstalls old versions of each formula that is upgraded with `brew upgrade`, and
  periodically performs additional cleanup every 30 days."

## Decision

- One helper, `installer_config.stable_uv_path`: `os.path.abspath`, absolute with links kept.
  All nine places and `scheduled_path` use it. The PATH a run gets is now the link's directory
  (`/opt/homebrew/bin`), which is also where Homebrew's other commands are.
- Installed units that named the old target are rewritten by the next installer run. Doctor's
  scheduler check now reads the program each installed systemd unit and LaunchAgent starts and
  says `degraded` with "rerun the installer" when that uv no longer exists (its existing unit
  check compared only time limits). The Windows task spec and cron are not read by this check.

## Uncertainty

- A uv installed as a real file (the standalone installer's `~/.local/bin/uv`) is unaffected.

## Files

- `scripts/installer_config.py`
- `scripts/install_control.py`
- `scripts/doctor.py`
- `tests/test_a_scheduled_run_keeps_the_uv_link.py`
- `CHANGELOG.md`
