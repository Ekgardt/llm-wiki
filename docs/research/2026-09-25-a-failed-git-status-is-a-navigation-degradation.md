# A failed git status is a navigation degradation

Date: 2026-09-25. Audit item B-41 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by test)

- `workspace_revision._git_run_outcome` raised `subprocess.CalledProcessError` when a Git command
  exited non-zero (a corrupt index, a checkout Git refuses). `code_navigation` computes the
  revision inside `except (OSError, ValueError, RuntimeError)` and turns those into a named
  `ERROR`/`revision_failed` answer; `CalledProcessError` is none of them, so the failure reached
  the caller as a generic error.
- Every other reader in `workspace_revision` that handles a failed Git run already catches
  `ValueError` (or `SubprocessError` together with `ValueError`).

## Source

- Python docs, `subprocess`, https://docs.python.org/3/library/subprocess.html (fetched 2026-09-25
  for B-37): the module's exceptions are subclasses of `SubprocessError` (`TimeoutExpired` is
  "Subclass of: `SubprocessError`"), not of `OSError` or `ValueError`.

## Decision

- A non-zero Git exit raises `GitCommandFailed(ValueError)` naming the command's label and exit
  code; the navigation's existing degradation handles it. The A-16 helper catches it as a
  `ValueError`.

## Files

- `scripts/workspace_revision.py`
- `tests/test_a_failed_git_status_is_a_navigation_degradation.py`
- `CHANGELOG.md`
