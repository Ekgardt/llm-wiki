# A tool breadcrumb waits in the background

Date: 2026-09-29. Status: implemented in the same change.

## What was measured (live vault, 2026-09-28)

- Losses in `logs/capture-failures.jsonl` (outcome `lost`): 371 on 2026-09-27 and 738
  on 2026-09-28. Leading reasons since 2026-09-27: 481 `TimeoutError: transaction
  mutation deadline or cancellation reached`, 356 `timed out waiting for the global
  Markdown writer gate`, 143 `TimeoutExpired` on the delegate `post_tool_capture.py`
  (killed at 3.5 s). Losses cluster: 169 minutes of 2026-09-28 had any, the worst
  32 in one minute (20:36), while four cores were busy with test runs.
- 108 `post-tool:` append attempts were refused `precondition_failed` and never
  retried: the loop in `_append_until_committed` retries a refused CAS attempt,
  but the 2.5 s budget (`daily_log_append.BREADCRUMB_APPEND_BUDGET_SECONDS`) ended
  first. `doctor` counts these as "refused attempts whose work never happened".
- Offered load is small: appends of all capture kinds peaked at 30 per minute and
  3 per second (p95 16 per minute) on 2026-09-28.
- One append costs about 0.2 s on an idle test vault, independent of the daily
  file's size (183 ms at 10 kB, 211 ms at 1.5 MB, median of 15): the first
  hypothesis, "a large daily file is rewritten", was wrong. The profile shows 18
  SQLite commits (each at least two fsyncs in rollback-journal mode) and 8 entries
  into the writer gate per append.
- A live writer may hold the gate for its whole lease, `_WRITER_LEASE_SECONDS` = 30.

So the losses are not throughput but latency: a hook that must finish inside the
host's 5 s cannot wait out a gate holder, a CPU-starved interpreter start, or both.

## Sources

- Claude Code hooks reference (read today, https://code.claude.com/docs/en/hooks):
  `"async": true` "runs in the background without blocking", and "Claude Code
  doesn't enforce the `timeout` on command hooks run with `async: true`". Available
  since 2.1.0 (Jan 2026, https://code.claude.com/docs/en/changelog); installed
  here: 2.1.283. A background hook's output is not used, so only hooks that return
  nothing to Claude qualify: `PostToolUse` and `PostToolUseFailure` capture, not
  `UserPromptSubmit`, which returns `additionalContext`.
- SQLite, Atomic Commit (https://www.sqlite.org/atomiccommit.html): a rollback-
  journal commit needs two flushes of the journal and one of the database; the time
  goes to disk I/O. This is why one append costs what it costs, and why fewer
  commits per append is the lasting throughput fix.
- PostgreSQL, Asynchronous Commit (https://www.postgresql.org/docs/current/wal-async-commit.html):
  group commit (`commit_delay`) amortises one flush over concurrent committers;
  asynchronous commit trades the last transactions for latency. Both are the
  standard answers for many small durable writes.
- SQLite result codes (https://www.sqlite.org/rescode.html): SQLITE_BUSY is
  transient and is answered by waiting.

## Alternatives

1. Run the two tool-capture hooks in the background and let their append wait out
   one full writer lease. Chosen. It removes the loss mechanism named above
   (a deadline shorter than a legitimate gate hold), costs no durability, changes
   no storage contract, and takes the capture's 0.3-3.5 s off every Edit, Write
   and Bash call the agent makes. Trade-offs: a breadcrumb may land a little later
   and, under a long hold, out of order (each carries its own timestamp and
   operation marker); background processes wait instead of dying, bounded by the
   budget (at the measured peak, 30 per minute for about 33 s, some 17 at once).
2. Group commit: hooks write a create-only intent file, one drainer appends them
   in one transaction. The lasting throughput fix, but it needs a drainer without
   a daemon and a new intent kind in Reliability v3 — an architecture change with
   its own research. Not needed for the measured load; recorded as the next step
   if the offered rate approaches the ~5 per second one writer can do.
3. Cut commits per append (18 today). Worth doing, but it is the transaction
   engine's recovery contract; measured separately.
4. Asynchronous durability (fewer fsyncs). Rejected: the operational databases are
   `synchronous=FULL` by contract, and captures are evidence.
5. A longer synchronous budget. Rejected: the host kills a synchronous hook at its
   5 s timeout, and the agent would wait on every tool call.

## Decision

- `integrations/claude-code/settings.json`: `PostToolUse` and `PostToolUseFailure`
  carry `"async": true` and pass `--background` to the adapter.
- `daily_log_append.BACKGROUND_APPEND_BUDGET_SECONDS` = one full writer lease plus
  one breadcrumb append (30 + 2.5 s). Basis: the longest a live writer may hold
  the gate, then the time one append is given anyway. Review if doctor reports
  overdue writers, or if a background capture is still lost to the gate.
- The adapter stops a background delegate at that budget plus the interpreter
  start it already allows; synchronous hosts (Codex, OpenCode, the prompt hook)
  keep the 2.5 s budget under their 5 s limit.
- Guard: tests pin that the two hooks are async and pass `--background`, that the
  background delegate gets the longer budget, and that a delegate held behind a
  writer for longer than the synchronous budget still lands its breadcrumb.
