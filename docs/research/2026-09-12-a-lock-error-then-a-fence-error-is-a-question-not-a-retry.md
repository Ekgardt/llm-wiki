# A lock error, then a fence error, is a question — not a retry

Dated 2026-09-12. The one problem I had named and not researched: the clean run
of `d8bb199` failed in `tests/test_blackboard.py::
test_multiprocess_status_reads_remain_coherent_during_claim_and_complete` while
six parity runs and an index build shared the machine. It passes alone in 102 s.
"Passes alone" is not a diagnosis, so here is one.

## The failure, verbatim

```
completion of worker/0/task/0 failed after 2 of 12 attempt(s) in 31.64s;
attempt 1: OperationalError('database is locked')
attempt 2: BlackboardFenceError('blackboard claim resource epochs changed')
```

## The facts under it

- `tests/test_blackboard.py::_under_contention` retries a call twelve times, but
  only while `_is_contention` says the error was contention: a `TimeoutError`, a
  SQLite `database is locked`, or a lost writer gate. A `BlackboardFenceError` is
  none of those, so attempt 2 ended the test — 2 of 12, not 12 of 12.
- The harness retries **`claim_task` and `complete_task` separately**. Attempt 2
  of the completion therefore replays the *same* `BlackboardClaim`, carrying the
  resource epochs captured when it was claimed.
- `blackboard.complete_task` starts with `_load_live_claim`, which refuses when
  the stored epochs no longer equal the claim's. So a replay after the epochs
  moved **cannot succeed, ever** — the twelve attempts were never going to help.
- The blackboard's writes already open with `begin_immediate`, so the classic
  deferred-transaction trap is not the cause here (see the sources below for why
  that mattered to check).
- `complete_task` publishes through `_append_once(..., key="id", value=claim.claim_id)`:
  the completion record is idempotent **by claim id**. A fresh claim has a fresh
  id, so a re-claim followed by a completion would append a *second* record and
  the test's `status["completed_tasks"]` would count the task twice.

## Research, current practice on this date

1. **Retry only what is idempotent, bound the budget, add jitter — and fix
   admission rather than lengthening the loop.** "Bound the total request
   deadline, retry only an idempotent transaction, add jitter, and surface a
   controlled overload error when the budget is exhausted… an ever-longer retry
   loop only hides overload"
   ([Fix SQLite "database is locked" under concurrent writes](https://oneuptime.com/blog/post/2026-09-08-fix-sqlite-database-is-locked-concurrent-writes/view)).
2. **A busy timeout does not cover every busy.** A `DEFERRED` transaction that
   meets the lock mid-transaction errors immediately without calling the busy
   handler, which is why `BEGIN IMMEDIATE` is the rule for a transaction known to
   write; and `SQLITE_BUSY` is a file-level conflict while `SQLITE_LOCKED` is
   object-level
   ([SQLite result codes](https://sqlite.org/rescode.html),
   [what to do about SQLITE_BUSY despite a timeout](https://lobste.rs/s/yapvon/what_do_about_sqlite_busy_errors_despite)).
   Checked: the blackboard already does this.
3. **A version conflict is not contention.** Optimistic concurrency requires
   re-reading the current version before acting again; replaying a request that
   carries a stale version is guaranteed to fail. That is the whole purpose of a
   fencing epoch, and it is why the retry above could not work.

## The diagnosis

Attempt 1 reported a lock error, which says **the outcome is unknown**, not that
nothing happened. Two sequences fit the message equally well, and the log does
not distinguish them:

- the completion landed and deleted the claim rows, and the error came from a
  later statement in the same call; or
- another writer's work moved the epochs while this caller was waiting.

Under the first, the work is done and the retry asks a question whose answer is
"already". Under the second, this claim is dead and the caller must claim again.
Either way, **replaying the same claim is the one thing that cannot help.**

## The decision

The harness stops treating an unknown outcome as a retry and starts treating it
as a question: after a swallowed contention error, a `resource epochs changed`
refusal is resolved by **reading back whether this claim's completion landed** —
the record is keyed by claim id, so the answer is exact — and a landed completion
counts as the success it is. Anything else still fails the test, loudly, with
every swallowed error attached.

Two alternatives, and why not:

- **Tolerate `BlackboardFenceError` as contention.** It would hide exactly the
  incoherence this test exists to catch.
- **Retry the claim-and-complete pair.** A fresh claim has a fresh id, so if the
  first completion had landed the task would be counted twice and the assertion
  would fail for a new reason.

What this does not change: the product. `complete_task`'s refusal is correct, the
fence stays, and `begin_immediate` was already right. What it changes is a test
that asked a dead claim the same question twelve times.

## Open, and honest

I did not reproduce the interleaving under instrumentation; the two sequences
above are both consistent with the recorded messages, and the read-back handles
both, which is why I did not need to choose between them to fix it. The load that
produced it — six parity runs and an index build on the same machine — is not the
load CI applies, and CI has not shown this failure.

Files: `tests/test_blackboard.py`,
`docs/research/2026-09-12-a-lock-error-then-a-fence-error-is-a-question-not-a-retry.md`.
