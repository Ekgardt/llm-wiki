# The install smoke checks the install, not the vault's history

Date: 2026-09-26. Audit 2026-09-26 A-10.

## Facts

- `scripts/install_smoke.py` raised `SmokeFailure` whenever doctor's overall
  status was `error`, and the installer then rolled the reinstall back. Doctor's
  `error` also covers vault state the installer does not own and often cannot
  fix by running again: a failed nightly (`scheduler`), a retained transaction
  (`transactions`), a dead queue task (`queue`), claims, captures, checkpoints.
  So one bad night blocked the one action that installs the fix.
- A smoke test, per https://en.wikipedia.org/wiki/Smoke_testing_(software)
  (fetched 2026-09-26), is "preliminary testing ... to reveal simple failures
  severe enough to ... reject a prospective software release", limited to
  whether "main functions of the software appear to work correctly".
- A scratch doctor run lists the check ids: environment, runtime, adoption,
  filesystem, transactions, queue, archives, claims, scheduler, capture, tools,
  backup, models, hooks, checkpoints, mcp, integrations, pyright, lsp,
  generation, run_deletion. `tools` and `hooks` read failure trails (history),
  not installed files.

## Decision

- The smoke fails on an `error` only in a check the installer owns:
  `environment`, `filesystem`, `adoption`, `mcp`, `integrations` — or when doctor
  says `error` and names no failing check. Any other `error` leaves the install
  in place and the smoke reports `degraded`, naming the checks, so the operator
  sees them; doctor still reports them afterwards.
- The doctor exit code must agree with its report: 0 ok, 1 degraded, 2 error.
- The MCP tool contract and the production imports are unchanged.

## Files

- `scripts/install_smoke.py`
- `tests/test_install_smoke.py`
- `tests/test_a_failed_smoke_names_what_failed.py`
- `tests/test_a_smoke_checks_the_install_not_the_vault.py`
- `CHANGELOG.md`
