# The advice reads the decisions

Dated 2026-09-14. Item 2.8 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

Every session opens with a "knowledge state" block
(`session_start_context._flush_line`). Today it said:

> Flush classifier: 74/74 sessions returned FLUSH_OK — classifier may be too strict
> (losing signal).

- The line reads `run/state.json → flush_tier_counts`, which only the old hook path
  (`flush_memory._append_flush_state`, `_record_empty_state`) updates. It was last
  updated 2026-09-07 and holds `{"ok": 74}`. That path also counts a missing,
  disallowed or empty transcript as `ok`.
- The capture path that actually classifies sessions today — the queue worker,
  `process_new_capture` — writes a decision file per session,
  `run/queue-results/capture-decision-*.json`, with its `tier`, and never touches
  the counter. Read on the live vault: 56 decisions, **23 ok, 22 major, 11 minor**.

So every agent is told, at the start of every session, that the classifier throws
away almost everything, when it keeps 59 % — advice built on a counter nothing
current writes.

## Practice on this date

- A health signal is derived from the system of record, not from a parallel counter
  that can drift from it: the decision files are the record of what was decided,
  and a counter maintained beside them is a cache of that record with no
  invalidation. SRE practice asks that an alert or advisory be actionable and
  reflect the system's real current behaviour, or it trains people to ignore it
  ([Google SRE: monitoring distributed systems](https://sre.google/sre-book/monitoring-distributed-systems/)).
- A session-start line costs tokens in every session (the 2026-08-29 note on what
  belongs there); a wrong line costs them and misleads.

## The decision

- `_flush_line` is computed from the decision files: the tiers of the most recent
  `MAX_DECISIONS_READ = 200` capture decisions, by modification time, each file read
  within a 64 KiB bound; a file that cannot be read is skipped. The threshold is
  unchanged (at least 5 decisions, more than 70 % `ok`).
- `flush_tier_counts` is no longer read. The old hook path still writes it; that path
  is not the one that runs, and removing its bookkeeping is not needed to stop the
  wrong advice.

Why not the alternatives:

- **Make the queue worker update the counter too.** Two writers of one number, and
  the 74 stale `ok` would still dominate for weeks.
- **Reset the counter.** It would drift again the next time a path changes.

Files: `scripts/session_start_context.py`,
`tests/test_the_advice_reads_the_decisions.py`,
`docs/research/2026-09-14-the-advice-reads-the-decisions.md`.
