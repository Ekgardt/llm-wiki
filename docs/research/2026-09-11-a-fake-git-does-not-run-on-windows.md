# A fake git does not run on Windows

Date: 2026-09-11. Trigger: the Windows full-suite shard 2 failed on every
Python version (runs 34618913527 on 4fac4da, 34627767725 on deec4ab) with one
test, `tests/test_impact_analysis.py::test_a_git_warning_on_stderr_is_not_a_diff_record`:
`ValueError: Git impact command failed: warning: Not a git repository`.
Linux and macOS pass; the local clean runs (Linux) passed.

## Cause

The test writes a `#!/bin/sh` script named `git` into a temporary directory
and prepends it to `PATH`. `impact_analysis._start_git` launches `["git", ...]`
with `shell=False`. On Windows `CreateProcess` resolves a bare program name
to an `.exe` only (Python `subprocess` documentation, "Windows Popen
Helpers": the search follows `CreateProcess` rules, which append `.exe`), so
the extension-less script is never found and the real `git.exe` runs in a
directory that is not a repository. The product code is right; the test is
POSIX-only by construction.

## Measured

A real repository reproduces the condition the test is about, on this
machine with Git 2.43: `core.autocrlf=true`, a committed file rewritten with
LF endings, `git diff --raw -z` prints
`warning: in the working copy of 'pkg/core.py', LF will be replaced by CRLF
the next time Git touches it` on stderr and exits 0. The same warning is
what the Windows runners printed in the PR30 runs this test was written for.

## Decision

The test drives the real Git in a real repository with `core.autocrlf=true`
instead of a fake executable: it exercises the actual stderr/stdout split on
every platform, and needs no platform skip. Other tests that write a
`#!/bin/sh` git are either POSIX-guarded already or passed on Windows, so
only this one changes.

Files: `tests/test_impact_analysis.py`.
