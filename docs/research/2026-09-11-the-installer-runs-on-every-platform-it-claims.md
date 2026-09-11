# The installer runs on every platform it claims

Date: 2026-09-11. Trigger: audit finding OPS-14. `CLAUDE.md` names Task
Scheduler, a user LaunchAgent and a systemd timer as the supported
schedulers, and the CI installer job ran on `ubuntu-24.04` and
`windows-2025` only, so the LaunchAgent path in `install_control.py` and
the install smoke test on macOS had no evidence; a regression there merged
green.

## Sources

1. `.github/workflows/tests.yml`: the `installer` job already carries a
   platform matrix with per-platform environments and state roots; the
   full-suite matrix already runs on `macos-15`, so the runner image and
   the `uv` setup are known to work there.
2. GitHub-hosted runner images: `macos-15` is a current Apple-silicon image.
   https://github.com/actions/runner-images

## Decision

The installer job gains `macos-15 / macos`, running the same installer,
config, smoke, injection and dependency-environment tests. `all-green`
already requires every installer job. The clean-install jobs
(`clean-production`, `clean-hybrid`, `clean-code-graph`) stay Linux-only for
now: they cost a full dependency sync each and are the next step if the
installer job is green on macOS.

Files: `.github/workflows/tests.yml`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
