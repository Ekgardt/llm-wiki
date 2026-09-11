# A clean install runs on every platform the contract names

Date: 2026-09-11. Trigger: the open half of audit finding OPS-14. The
installer test job runs on all three platforms since this morning, but
`clean-production` — the job that installs the production profile into an
empty environment, asserts pytest is absent and runs
`scripts/install_smoke.py` end to end — still ran on `ubuntu-24.04` only,
so a clean production install on Windows or macOS had no CI evidence.

## Sources

1. `.github/workflows/tests.yml`: `installer` already uses an `include`
   matrix over `ubuntu-24.04`, `windows-2025`, `macos-15`; the job names
   the branch protection knows (`timing::clean::production-py3.10`,
   `-py3.14`) must not change, because PR checks are matched by name.
2. `tests/test_ci_policy.py::_assert_clean_profile`: the clean profiles
   must stay isolated (`--locked --no-sync`, a private
   `UV_PROJECT_ENVIRONMENT`, no pytest in the environment).
3. Cost: one more Windows and one more macOS runner of about the same
   length as the installer job (under 10 minutes); Python 3.13 on both,
   the interpreter the installer job already uses there.

## Decision

`clean-production` becomes an `include` matrix: the two Linux entries keep
their labels (`py3.10`, `py3.14`) and names; `windows-2025` and `macos-15`
are added with Python 3.13 and labels `windows-py3.13`, `macos-py3.13`.
`UV_PROJECT_ENVIRONMENT` and the state root are keyed by label. The
policy test asserts the matrix covers the three runners and keeps the
Linux entries as they were.

Files: `.github/workflows/tests.yml`, `tests/test_ci_policy.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
