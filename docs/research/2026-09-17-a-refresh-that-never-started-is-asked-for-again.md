# A refresh that never started is asked for again

Date: 2026-09-17. Audit 3, code intelligence, finding B2.

Files: `scripts/mcp_server.py`,
`tests/test_a_refresh_that_never_started_is_asked_for_again.py`

## What was found

`_request_repository_refresh` and `_request_worktree_follow` start a detached
index process once per (repository, commit) or per checkout. Both write the
"requested" mark *before* they spawn, which is right — it is what keeps two
concurrent questions from starting two processes. But the mark stays when the
spawn fails. `memory_state.spawn_detached` documents the failure: "Returns the
spawned PID, or None if spawn failed." The first answer says `spawn_failed`;
every later answer in that server process says `already_requested`, which is
false, and nothing retries until the server restarts. For the shared HTTP
server that is days.

## Sources

- `scripts/memory_state.py`, `spawn_detached` docstring, quoted above.
- Python `subprocess` documentation
  (https://docs.python.org/3/library/subprocess.html#exceptions): "The most
  common exception raised is OSError. This occurs, for example, when trying to
  execute a non-existent file." A spawn can fail for transient reasons too
  (process limit, memory), which is why "failed once" must not mean "never".

## Decision

The mark is still written before the spawn. When the spawn returns no pid the
mark is taken back under the same lock — only if it is still this request's
mark — so the next question tries again and says what happened. A spawn that
keeps failing costs one failed `Popen` per question, and each answer says
`spawn_failed` truthfully.
