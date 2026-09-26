# Every process settles its inference at exit, not only the server

Date: 2026-09-25. Audit item C-18 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `inference_threads` exists because a daemon thread in the middle of model
  inference when the interpreter finalizes aborts the process (exit 134,
  `terminate called without an active exception`; see
  `docs/research/2026-09-14-no-model-running-at-exit.md`). `settle` stops and
  joins those threads.
- Only `mcp_server` calls `settle` (`_settle_inference`). A one-shot process —
  the search CLI, a grounded query from the command line, the nightly steps —
  whose deadline abandons a rerank still has that thread running when `main`
  returns, and nothing waits for it.

## Source

- Python `atexit`, https://docs.python.org/3/library/atexit.html (fetched
  2026-09-25): exit handlers run "upon normal program termination (for instance,
  if `sys.exit()` is called or the main module's execution completes)"; since
  3.12 a handler may not start a thread. Joining one is allowed.

## Decision

- `inference_threads.start` registers, once per process, an exit handler that
  settles the running inference threads within `EXIT_SETTLE_SECONDS` = 30 s, the
  same bound the server uses. The server's own explicit call stays; the handler
  then finds nothing running.

## Files

- `scripts/inference_threads.py`
- `tests/test_every_process_settles_its_inference_at_exit.py`
- `CHANGELOG.md`
