# Doctor names installed hooks that are older than the release

Date: 2026-09-25. Audit item C-1 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on this machine)

- The Claude settings on this machine still pass
  `--delegate precompact_capture.py` on `PreCompact`; that module was deleted on
  2026-09-17. It works only because the adapter keeps the retired names as a
  compatibility map (`integration_adapter.CAPTURE_DELEGATES`).
- `merge_claude_settings.py` replaces every hook entry that names one of our
  scripts, including the retired names, when the installer runs. Nothing runs it
  after an update: `self_update` reports `resources: rerun_installer` for a
  change under `integrations/`, and its contract says "a maintenance pass must
  not write the operator's shell profile or agent configuration by itself".
- `doctor`'s integration check only asks whether a host's config names
  `integration_adapter.py`; an outdated entry reads as `ok`.

## Source

- Claude Code settings, https://code.claude.com/docs/en/hooks (fetched
  2026-09-25): hooks live in the user's settings file and each entry is the
  command it runs — the only place the outdated flag can be seen is that file.

## Decision

- The Claude host check also looks for the retired delegate names; when it finds
  one it is `degraded` with "Installed Claude hooks predate this release; rerun
  the installer to refresh them." The read is bounded and read-only like the
  existing marker check. The compatibility map stays until no supported install
  can carry the old names.
- The maintenance contract is kept: nothing rewrites agent configuration by
  itself.

## Files

- `scripts/doctor.py`
- `tests/test_doctor_names_hooks_older_than_the_release.py`
- `CHANGELOG.md`
