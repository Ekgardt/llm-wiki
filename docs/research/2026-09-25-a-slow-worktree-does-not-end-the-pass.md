# A slow worktree does not end the pass

Date: 2026-09-25. Audit item B-37 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `repository_worktrees._git` ran Git with a timeout of its own and let
  `subprocess.TimeoutExpired` escape. It is called by `require_indexing_wanted`, which every index
  and refresh runs first, and by the worktree follow. `_refreshed_row` in the nightly `refresh-all`
  catches `RepositoryIndexRefused` and `TimeoutError`, so one slow checkout ended the whole pass,
  and `retire` the same way.
- `repository_index._git_completed` already turns that timeout into the named refusal
  `repository_git_probe_timed_out` (2026-09-17, "one checkout does not end the pass"); the worktree
  module had a second copy of the runner without it.

## Source

- Python docs, `subprocess`, https://docs.python.org/3/library/subprocess.html (fetched 2026-09-25):
  `TimeoutExpired` is a "Subclass of: `SubprocessError`" — "Raised when a timeout expires while
  waiting for a child process." It is not a `TimeoutError`, which is why an `except TimeoutError`
  deferral never saw it.

## Decision

- The worktree module's `_git` is the index's `_git_completed`: one runner, one refusal. There is
  no other copy of the runner in `scripts/` (checked with grep).

## Files

- `scripts/repository_worktrees.py`
- `tests/test_a_slow_worktree_does_not_end_the_pass.py`
- `CHANGELOG.md`
