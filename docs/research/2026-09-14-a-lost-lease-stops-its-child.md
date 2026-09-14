# A lost lease stops its child

Dated 2026-09-14. A guess at the end of `docs/AUDIT-2026-09-14-2.md`, partly confirmed by
a read-only audit today. The research before the fix.

## What was found

- A worker runs each claimed task in a child process (`memory_queue._run_processor_child`)
  under a lease heartbeat (`_LeaseHeartbeat`). When renewal fails for good, the heartbeat
  sets `error` and stops (`_LeaseHeartbeat._run`). The parent notices only after the child
  has finished: `_processor_result` waits for the child up to the worker's deadline, and
  `_run_and_settle` checks `_heartbeat_lost` afterwards.
- The queue result itself is safe: the child holds no lease, and the parent skips
  settlement when the heartbeat was lost; `publish_result` and `acknowledge` check the
  lease token. But the child's side effects are not fenced — a flush appends to the daily
  log, a compile writes pages — and once the lease has expired another worker may run the
  same task. Both run to completion; operation ids reduce the duplicates, they do not
  remove the double model call or every double write.
- The code graph: `_run_processor_child` ← `run_worker` default `processor_runner`;
  `_await_child_message` ← `_child_ready_handshake`, `_child_result_frame`. Custom runners
  in tests take `(processor, task, timeout)`; `_run_processor_inline` cannot be
  interrupted at all.
- `docs/research/2026-09-14-a-lost-lease-is-known-by-its-expiry.md` made the heartbeat
  report the loss by the lease's expiry; nothing acted on that report until the child
  finished.

## Practice on this date

- A holder that loses its lease must stop acting on it: Kubernetes leader election's
  `OnStoppedLeading` callback exists so the former leader stops its work, and client-go
  documents that losing the lease "should be treated as a fatal error" for the work it
  guarded ([client-go leaderelection](https://pkg.go.dev/k8s.io/client-go/tools/leaderelection)).

## The decision

- The child runner waits for its child in slices of at most one second and checks the
  lease between them. When the heartbeat has failed, it stops the child's process tree
  (the same `_ChildRun.stop` the deadline uses) and returns `lease_lost`; the parent then
  counts the task failed and settles nothing, as before.
- Only the default child runner is made interruptible; injected runners keep their
  three-argument form and their current behaviour.

Files: `scripts/memory_queue.py`, `tests/test_a_lost_lease_stops_its_child.py`,
`docs/research/2026-09-14-a-lost-lease-stops-its-child.md`.
