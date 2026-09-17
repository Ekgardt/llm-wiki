# One checkout does not end the pass

Dated 2026-09-17. Third audit, finding G-H2. The research before the fix.

Files: `scripts/repository_index.py`,
`tests/test_one_busy_or_broken_checkout_does_not_end_the_pass.py`.

## What was found

- `repository_index._refresh_row`, `_adopted_vault`, `repository_worktrees._followed` and
  the MCP `index` tool (`mcp_server._repository_index_call`) turn two things into a row of
  the report: the named refusal `RepositoryIndexRefused`, and `TimeoutError` (2026-09-14,
  `docs/research/2026-09-14-a-pass-that-keeps-its-budget.md`). Everything else ends the
  whole `refresh-all` pass with a traceback: no report, every checkout after it skipped, no
  worktree followed.
- Four errors of a single checkout are not of those two kinds. Each was read in the code;
  the first was reproduced on a temp state root with a thread rewriting one file
  (`CorpusChanged: corpus never held still for one pass`):
  1. `corpus_snapshot.CorpusChanged` (a `RuntimeError`) from `_collect`, which translates
     only `OSError` and `ValueError`. An agent writing in a registered worktree at night is
     the ordinary case.
  2. `subprocess.TimeoutExpired` from `_git_text` (fixed 10 s). The Python documentation:
     "Subclass of SubprocessError, raised when a timeout expires while waiting for a child
     process." and, for `run`: "If the timeout expires, the child process will be killed
     and waited for. The TimeoutExpired exception will be re-raised after the child process
     has terminated." (https://docs.python.org/3/library/subprocess.html, fetched
     2026-09-17). It is not a `TimeoutError`, so no caller's deferral catches it.
  3. `FileNotFoundError` from `_verified_source_manifest` when a registration's tree is
     gone: `path.read_bytes()` is outside any translation. `_covered` (the listing) already
     guards it; detection and refresh do not.
  4. `doctor.MaintenanceFenceLost` (a `RuntimeError`) from `run_fenced`: the heartbeat's
     `__enter__` checks the owner and its `__exit__` releases it, and both raise it when the
     lease was taken. Raised from `__exit__`, it also replaces the `TimeoutError` the
     cancelled build was already carrying.

## Practice on this date

- The module's own contract is the practice: "A named, fail-closed refusal. `reason` is
  stable; the message explains." Every caller already handles that type. The defect is that
  four sources do not speak it. Translating at the source fixes every caller at once; a
  broader `except` in three callers would hide programming errors and still miss the fourth
  caller (MCP).

## The decision

- `_collect`: `CorpusChanged` becomes the refusal `repository_changed_during_capture`.
- `_git_text`: `subprocess.TimeoutExpired` becomes the refusal
  `repository_git_probe_timed_out`.
- `_verified_source_manifest`: an `OSError` reading the file becomes the existing refusal
  `repository_index_unreadable`.
- `run_fenced`: `MaintenanceFenceLost` becomes the existing fence refusal
  `refresh_owned_elsewhere`, with the reason `fence_lost`. All three users of the fence
  (refresh, follower, retention) already report a refused fence.
- No caller's `except` is widened. No path, env contract or runtime location changes.
