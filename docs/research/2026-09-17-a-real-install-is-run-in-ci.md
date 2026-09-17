# A real install is run in CI

Dated 2026-09-17. Finding I-C1 of the third audit (CI gap, confirmed by reading
`.github/workflows/tests.yml`). The research before the fix.

## What was found

- No job ever ran `install.sh` or `install.ps1`. The `installer` job runs pytest files that
  extract single functions or sections of those scripts and drive `install_control` with
  fake runners.
- Three defects lived behind that gap at the same time: a fresh vault never adopted the
  Reliability V3 queue (I-A1, issue #17 again), the Windows installer had no adoption step
  at all (I-A2), and the POSIX one used `mapfile` and a bare empty-array expansion, which
  the bash macOS ships (3.2) does not have (I-A5).
- Running the installer here for the first time immediately produced a fourth, worse one:
  even with I-A1 fixed, the installer's own runtime sync made the vault unadoptable before
  the adoption step ran. See
  `docs/research/2026-09-17-the-queue-is-adopted-before-anything-writes-to-it.md`.

## Practice on this date

- Measured locally: a full `install.sh` run against a `git clone` of the branch, a temporary
  `HOME`, a temporary state root and `MEMORY_LLM_PROVIDER=fake` finishes in a few minutes
  and needs no secret and no provider. The machine's own crontab was kept out of it with a
  stub `crontab` on `PATH`; a hosted runner is disposable and needs no such stub.
- The installer needs a git checkout: `build_release_identity` runs `git rev-parse HEAD` and
  fails with `install_release_identity_failed` on an unpacked tarball. `actions/checkout`
  gives that.
- Scheduler backends a hosted runner can actually register: `ubuntu-24.04` has no systemd
  user manager (`select_scheduler_backend` then refuses `native` and says to select cron),
  and the macOS LaunchAgent domain is `gui/<uid>`, which needs an Aqua session. So the POSIX
  job asks for `--scheduler cron`, the documented degraded fallback; the native backends stay
  covered by the existing `installer` job's unit tests. Windows registers its Task Scheduler
  entries natively.
- macOS runs the installer as `/bin/bash ./install.sh`, which on macos-15 is bash 3.2 — the
  shell the contract names, and the one `tests/test_the_installer_says_what_it_needs.py`
  checks the syntax against.

## The decision

One new job, `install-end-to-end`, on ubuntu-24.04, macos-15 and windows-2025, inside the
workflow's existing concurrency group and naming convention (`timing::installer::…`), with a
30-minute bound. It creates a home directory of its own before `HOME`/`USERPROFILE` move, so
the runner's own configuration is never touched, runs the installer, and then asks the
installed vault `repair_installed_memory.py --check --json`, which exits 0 only for an
adopted vault — the outcome that had been broken three times. `all-green` depends on it. A
test in `tests/test_readme_i18n.py` holds the job's shape, as it already does for the
Pyright job.

Files: `.github/workflows/tests.yml`, `tests/test_readme_i18n.py`,
`docs/research/2026-09-17-a-real-install-is-run-in-ci.md`.
