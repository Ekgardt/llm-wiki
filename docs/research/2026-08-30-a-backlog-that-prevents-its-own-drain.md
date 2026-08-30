# A backlog that prevents its own drain

Dated 2026-08-30. Written before changing the project-checkpoint queue,
because the change is a design decision about admission and flow control.

## What is on disk right now

`run/state.json` is 6.7 MB. Of that, **4.8 MB is
`project_checkpoint_pending`**, and 3.7 MB of that is one queue: 2 537 pending
checkpoints for the `llm-wiki` project. The oldest is stamped
`2026-08-29T00:09:47`. The newest is minutes old. Sampled every 20 seconds for
two minutes with no session activity, the length did not move.

`logs/hook-errors.log` names the mechanism, 1 603 times and still arriving:

```
project checkpoint: TimeoutError: Could not acquire state lock: run/state.json.lock
```

Per day: 5 on 08-25, 13 on 08-26, 182 on 08-28, **1 338 on 08-29**, 65 by
11:51 on 08-30.

## How it got wedged

The queue is bounded in intent. `MAX_PENDING_CHECKPOINT_ITEMS = 40` carries a
comment recording an earlier measurement of the same class of failure — 136
events, 184 KiB, a reader that refuses `state.json` over 256 KiB, and two
health checks that reported nothing.

But 40 is a **flush trigger inside the drain**, not an admission cap.
`_enqueue_pending_events` appends with no limit at all. The bound only applies
if the drain runs.

Between 2026-08-28 14:47 and 2026-08-29 21:27 the drain could not run: 2 728
`ProjectJournalReadError: project journal exceeds 1000 event lines`. That
outage is fixed — journal rotation landed on 08-29 and the error stopped at
21:27 that day. The backlog it created did not go away, because of this:

- `_claim_pending_state` claims and copies the **entire** queue, not a window.
- `_observe_until_checkpoint` then replays the entire queue.
- One drain cycle therefore writes `state.json` — all 4.8 MB of it — three
  times: claim, persist, commit.
- Every one of those writes takes the lock, and the drain asks for it with
  `lock_timeout=0.5`.

So the cost of one drain cycle is proportional to the backlog, while the time
allowed to pay it is fixed. The bigger the backlog, the surer the timeout; the
surer the timeout, the bigger the backlog. The original cause is repaired and
the system is still stuck, which is the signature of this shape.

Two more consequences fall out of the same 4.8 MB file: 39 orphaned
`.state.json.*.tmp` files, 272 MB, all complete JSON — fully written and
fsynced, then abandoned when the writing process was killed before the rename.
And a `UserPromptSubmit` hook that times out at 5 s.

## What the field says

"An unbounded queue is a silent promise to process all work eventually — a
promise the system cannot keep under sustained overload. A bounded queue makes
the constraint explicit and forces a decision at the point where work cannot be
accepted." The standing advice is to always set bounds, even generous ones, and
to monitor queue depth rather than discover it.

Drop-oldest is the usual overflow policy and is explicitly **wrong here**:
these are project-handoff observations, not telemetry samples where a stale one
is worthless. Losing them silently is the failure mode this vault exists to
prevent.

The applicable pattern is instead the standard fix for a consumer whose cost
scales with the backlog: consume a **bounded window** per cycle, so that
recovery time is linear in the backlog rather than quadratic, and a backlog can
never make its own drain impossible.

## The decision

Claim, replay and flush at most one bounded window from the head of the queue
per drain cycle, instead of the whole queue.

- The window is `PENDING_CLAIM_WINDOW = MAX_PENDING_CHECKPOINT_ITEMS`, so
  normal operation — a queue at or under 40 — behaves exactly as it does today.
- The claimed items stay a prefix of the queue, so the existing prefix
  validation in `_commit_pending_state` and the ordering guarantee are
  unchanged.
- `_drain_project_checkpoints` already loops until a cycle reports no work, so
  a backlog drains window by window in that same loop.
- Nothing is dropped, no new file, no new database, no schema change.

This is not a cap on admission. It removes the coupling between backlog size
and drain cost, which is what makes the wedge self-sustaining. Whether an
admission cap should also exist is a separate question with a lossy answer, and
it belongs to the owner, not to me.

## What this does not fix

`state.json` is rewritten whole on every update, so the cost of one write is
proportional to total state, not to the change. With the queue drained that is
about 10 KB and irrelevant. Under any future backlog it returns. Moving the
queue out of `state.json` is an architectural change and needs the owner's
sign-off.

The 39 orphaned temp files are a separate defect in `atomic_write`: a process
killed between fsync and rename leaves the staged file forever, and nothing
sweeps them.

## Sources

- [Pattern: Backpressure / Flow Control — Battle-Tested Patterns](https://totoro-jam.github.io/battle-tested-patterns/patterns/backpressure/)
- [.NET 8 Channel-Based Queue: Bounded Capacity, Backpressure & Poison Job Handling](https://www.dotnet-guide.com/articles/dotnet-channels-queue-backpressure/)
- [Backpressure Patterns — Flow Control for Resilient Distributed Systems](https://codelit.io/blog/backpressure-flow-control)
- [Backpressure Patterns in Distributed Systems — Algoroq](https://www.algoroq.io/blog/backpressure-distributed-systems/)
- [Managing Backpressure in Async AI Services](https://dasroot.net/posts/2026/02/managing-backpressure-async-ai-services/)
- [Storage resilience: atomic writes, safer temp cleanup, repair/restore tools (opencode #7733)](https://github.com/anomalyco/opencode/issues/7733)
- [Better File Writing in Python: Embrace Atomic Updates](https://sahmanish20.medium.com/better-file-writing-in-python-embrace-atomic-updates-593843bfab4f)
