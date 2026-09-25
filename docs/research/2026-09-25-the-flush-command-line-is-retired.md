# The flush command line is retired

Date: 2026-09-25. Audit item C-4 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- Captures reach classification through the adapter and the queue worker:
  `integration_adapter` publishes a capture intent and a `flush` task, and
  `flush_memory.run_capture_worker_once` / `process_new_capture` classify it.
- `flush_memory.main` — the command line `python scripts/flush_memory.py
  --transcript ... --event ...` — is called by nothing in `scripts/`,
  `benchmark/`, `integrations/`, the plugins or the installers; only tests run
  it. A reachability pass over the module (definitions referenced from any other
  product module, then everything they reference) leaves 37 definitions reachable
  from `main` alone: argument parsing, the transcript tail reader and its path
  allowlist, `summarize_with_llm` with its own "enqueue when no provider" branch,
  the dedupe window, the CLI's daily append and state record, its compile trigger
  and its feedback call.
- `CLAUDE.md` §6 says "If none available, the call is enqueued in
  `run/queue.sqlite3`". Only `summarize_with_llm` did that, and nothing calls it.
  The live path waits instead: a capture whose provider does not answer is a
  stated one-hour wait (`docs/research/2026-09-25-a-silent-provider-is-a-wait-not-a-failure.md`).

## Source

- Google SRE book, "Simplicity", https://sre.google/sre-book/simplicity/
  (fetched 2026-09-25): "every line of code changed or added to a project creates
  the potential for introducing new defects and bugs", and code kept although it
  is no longer used is "a metaphorical time bomb waiting to explode". The CLI's
  queue branch is such code: a documented behaviour that no path performs.

## Decision

- The command line and every definition reachable only from it are removed from
  `flush_memory.py`, with the tests that exercised only them. What the worker and
  the classification stand use stays.
- `CLAUDE.md` and `AGENTS.md` §6 say what happens now: with no provider the
  capture task waits and is retried; nothing else is queued for later.

## Files

- `scripts/flush_memory.py`
- tests that ran only the command line
- `CLAUDE.md`, `AGENTS.md`
- `CHANGELOG.md`
