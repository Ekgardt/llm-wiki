# A failed reinstall puts the old one back

Date: 2026-09-25. Audit item B-31 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `install_control._install_from_args` uninstalls an "outgrown" install (a rerun that asks for
  fewer or other resources) before it installs the new set. When the new install failed, the
  machine was left with neither: no scheduler and no hooks.
- `install_smoke.main` printed only `install smoke failed: <ExceptionType>`, so any doctor
  `error` stopped the install with "RuntimeError" and nothing to act on. All its own raises carry
  fixed messages written in this module.
- Found beside it (class of B-30): `_requested_resources` and `_resources_from_record` resolved
  `args.uv_path` before the new `stable_uv_path` saw it, pinning the link's target again.

## Source

- Microsoft Azure Architecture Center, "Compensating Transaction pattern",
  https://learn.microsoft.com/en-us/azure/architecture/patterns/compensating-transaction
  (fetched 2026-09-25): "Implement a compensating transaction that undoes the effects of
  completed steps in the original operation." and "Compensating transactions don't always work.
  Define the steps in a compensating transaction as idempotent commands so that you can repeat
  them if the compensating transaction itself fails."

## Decision

- A failed install after a replacement reinstalls the previous manifest's resource set (the same
  idempotent `install_resources`), then re-raises the original failure; a restore that fails
  raises with the original as its context.
- The smoke raises `SmokeFailure` with its own message; the installer prints that message, and a
  doctor `error` names the failing check ids and the command that shows why. Foreign exceptions
  are still named by type only, since their text is not ours.
- Both uv path sites use `stable_uv_path`.

## Uncertainty

- Whether a doctor `error` should stop a reinstall at all is left as it is: the smoke exists to
  prove the installed product works. A-9 removed the main false `error` (budget exhaustion).

## Files

- `scripts/install_control.py`
- `scripts/install_smoke.py`
- `tests/test_a_rerun_may_ask_for_fewer_things.py`
- `tests/test_a_failed_smoke_names_what_failed.py`
- `CHANGELOG.md`

## Follow-up the same day (CI run 36182427739)

- Fact: on the Ubuntu runners `XDG_CONFIG_HOME` is set, so the installer tests that pass a temporary `home` wrote the OpenCode plugin into the runner's real `~/.config`; the new restore test, running after them, found that file and refused it (`install_resource_ownership_ambiguous`). Locally the variable is unset and the test passed.
- Decision: an autouse fixture in `tests/conftest.py` unsets it for every test; reproduced green locally with a fake `XDG_CONFIG_HOME`, into which nothing was written.
- File: `tests/conftest.py`.
