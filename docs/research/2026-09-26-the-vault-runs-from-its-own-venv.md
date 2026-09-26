# The vault runs from its own .venv

Date: 2026-09-26. Audit 2026-09-26 B-26.

## Facts

- `install.sh`/`install.ps1` synced dependencies into whatever
  `UV_PROJECT_ENVIRONMENT` the installing shell had. The scheduled units, the
  agent hooks and the MCP server run `uv run` in the vault and carry no such
  variable, so they used `<vault>/.venv` — empty or stale after such an install.
- uv's environment reference (https://docs.astral.sh/uv/reference/environment/,
  fetched 2026-09-26): `UV_PROJECT_ENVIRONMENT` "Specifies the path to the
  directory to use for a project virtual environment" — it is read by every uv
  project, not only this one.
- The installer already has one channel that reaches units, hooks and profiles
  (`integration_hook_config.provider_environment`), but it also writes the shell
  profile, the Windows user environment and Claude's settings `env`; a
  `UV_PROJECT_ENVIRONMENT` there would hand every other uv project on the machine
  the vault's environment.

## Decision

- The vault has one environment, `<vault>/.venv` (already the path
  `install_control` treats as the vault's virtualenv). `installer_config sync-args`
  always plans it and reports a different `UV_PROJECT_ENVIRONMENT` as
  `ignored_environment`; both installers print a warning naming it.
- Nothing persists `UV_PROJECT_ENVIRONMENT`.

## Files

- `scripts/installer_config.py`
- `install.sh`
- `install.ps1`
- `tests/test_the_vault_runs_from_its_own_venv.py`
- `CHANGELOG.md`
