# A join has a bound, and a benchmark does not grade itself

Date: 2026-09-11. Trigger: audit findings OPS-18, OPS-19, M9 and M11 —
four small findings with one shape: a wait or a measurement that says
more than it proves.

- OPS-18: the two queue heartbeat threads (`memory_queue._SourceFenceHeartbeat`,
  `_LeaseHeartbeat`) are joined without a timeout; the thread may sit inside
  SQLite for `queue_busy_ms` (5 s), so the wait is bounded only by a
  constant in another module. `operational_ownership` joins its heartbeat
  with a timeout and refuses by name (`owner_heartbeat_stop_timeout`).
- OPS-19: the state-lock steal after 30 s. Re-read 2026-09-11:
  `memory_state._await_lock_turn` consults the age only to decide whether
  to look at the owner; an owner that is alive or unknown (EPERM) is waited
  for, and only a dead owner's lock is retired through `retire_stale_lock`
  with judged bytes (OPS-07, OPS-08). The steal the finding feared needs a
  dead-looking live owner, which the three-state probe no longer produces.
- M9: three tests sleep and then assert that a worker is still blocked
  (`tests/test_search_ranking.py:561`, `tests/test_claims.py:393, 435`).
  Each test also asserts, after the release, an outcome that is only
  possible if the worker was blocked: the freshness probe returns False
  only when it read the manifest written after the release; the fresh
  rebuild's candidates survive only when the stale rebuild wrote first;
  the provider sees the new page only when it ran after the write. The
  negative sleep adds nothing the outcome does not already prove, and it
  is the assertion that fails on a stalled machine for the wrong reason.
- M11: `benchmark/run_scale_matrix._run_exact_cell` computes the ground
  truth with the same call as the result, so `recall_at_10/50` is 1.0 by
  construction, and computes an adoption gate it then overwrites;
  `run_smoke` imports numpy only to write `_ = np`.

## Sources

1. `operational_ownership._join_owner_heartbeat` / `_require_heartbeat_stopped`:
   the shape of a bounded join in this repository.
2. `reliable_memory.DEFAULTS`: `queue_heartbeat_seconds = 40`,
   `queue_busy_ms = 5000`; a join bound of twice the heartbeat (80 s) sits
   far above the busy wait, and a floor of 10 s keeps a test that uses a
   1 s heartbeat above the busy wait too.
3. `docs/research/2026-09-10-every-hang-bound-in-the-tests-comes-from-one-place.md`
   (the negative-wait class) and `tests/test_retrieval_partial_on_expiry.py:55-64`
   (why "still blocked after N ms" failed on Windows py3.10).
4. `run_scale_matrix._metric_provenance`: a metric that is `None` carries a
   named reason, so an unmeasured recall is reportable without a number.

## Decision

- Both heartbeat `stop()` methods join with `max(2 × heartbeat, 10 s)` and
  raise `QueueOperationError("heartbeat_stop_timeout")` if the thread is
  still alive; one test drives a thread that ignores its stop event.
- OPS-19 is recorded as covered by OPS-07 and OPS-08.
- The three sleep-and-assert-blocked pairs are removed; the outcome
  assertions stay as the proof.
- The exact cell reports `recall_at_10/50 = None` with provenance reason
  `ground_truth_backend`; the discarded truth call and the overwritten
  adoption gate are gone; `run_smoke` no longer imports numpy for nothing.

Files: `scripts/memory_queue.py`, `tests/test_queue_heartbeat_stop.py`,
`tests/test_search_ranking.py`, `tests/test_claims.py`,
`benchmark/run_scale_matrix.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
