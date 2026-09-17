# An update says what it did not bring into force

Dated 2026-09-17. Findings I-B3 and I-B4 of the third audit (low, suspected). The research
before the fix.

## What was found

- `self_update.BASELINE_SYNC_COMMAND` is `uv sync --locked --inexact --no-default-groups`.
  `--inexact` is deliberate and recorded
  (`docs/research/2026-09-14-an-update-that-keeps-what-is-installed.md`): an exact sync would
  uninstall 93 packages on the live vault, torch and the models among them. The consequence
  the audit names is real all the same — a package that reaches the vault only through an
  optional extra (`hybrid`, `code-graph`, `reranker`, `semantic`) stays at its old version
  when the lock moves, and `uv run --locked --no-sync` does not notice, because it compares
  the lock with `pyproject.toml`, not with what is installed.
- An owned resource — a systemd unit, a launchd plist, a Windows task setting, an agent hook
  block, the OpenCode plugin — is rendered by the installer, not by the nightly. A fix to any
  of them reaches an installed vault only when the operator reruns the installer. Nothing
  says so.
- Both facts are invisible today: `scheduled_nightly._update_code` logs only
  `status (reason)` and `detail`, so even the existing `dependencies: stale` never reaches
  the report the operator reads.

## Practice on this date

- uv's `--inexact` is documented as "Do not remove extraneous packages present in the
  environment" (`uv sync --help`, this machine, uv 0.12.3). It says nothing about upgrading
  packages that the current selection does not name — and the current selection is the base
  project, without extras.
- Automatically syncing every installed extra unattended would mean a nightly pass that can
  download the CUDA stack the `reranker` extra pins (73 `nvidia-*` packages in `uv.lock`).
  A maintenance pass with a 3-hour limit must not decide that on its own.
- Re-rendering owned resources unattended is worse: those writes touch the operator's shell
  profile, agent configuration and scheduler, which the install ownership transaction owns
  precisely so that they are never written outside an explicit install.

## The decision

The update does not do either thing silently and does not stay silent about either. Its
outcome gains two fields, and the nightly step logs them:

- `extras`: the optional extras whose packages are installed here, named only when the
  update actually moved `uv.lock`. The operator resyncs the ones they want.
- `resources`: `rerun_installer` when the update changed a file the installer renders owned
  resources from (`scripts/install_control.py`, `scripts/installer_config.py`,
  `scripts/integration_hook_config.py`, `scripts/install-scheduled-tasks.ps1`,
  `integrations/`), otherwise `current`.

An extra counts as installed when every distribution it names is present, so the check
cannot be fooled by one shared package. The one line outside this area is
`scheduled_nightly._update_code`, which now logs the two fields.

Files: `scripts/self_update.py`, `scripts/scheduled_nightly.py`, `docs/USER-GUIDE.md`,
`tests/test_an_update_says_what_it_did_not_bring_into_force.py`,
`docs/research/2026-09-17-an-update-says-what-it-did-not-bring-into-force.md`.
