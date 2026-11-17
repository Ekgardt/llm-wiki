# Queue heartbeat

Owner: `scripts/llm_wiki/queue_heartbeat.py`. Two existing classes own the
thread, the stop signal and the renewal error. Source fence additionally
protects the current value with a lock. Narrow Protocols describe the queue
methods that are needed; SQL, fencing and the wait implementation stay with the queue.

Dependencies: the standard library, queue_contracts, reliability_defaults and
lease_renewal. Importing the executor, storage or durability is forbidden. The old
queue_core.LeaseHeartbeat and SourceFenceHeartbeat are the same class objects,
so existing stop substitutions keep their effect. The stop floor belongs to
the new module; tests change it there. It is an internal parameter, not a setting.

Values, ownership start time, intervals, the busy-timeout budget, daemon
thread names, the lock, the error publication order and the bounded join are preserved. After join
is_alive is checked; a live thread raises heartbeat_stop_timeout. Killing
the process still does not guarantee graceful cleanup of the daemon thread.

The complete packet is listed in queue-heartbeat-context.json: dependency sources,
contracts, documentation and all own tests. The limit is 100000 o200k_base
tokens. Tests run separately: `pytest tests/modules/queue_heartbeat -q`.
Integration is additionally checked by the tests of the queue, the compile controller,
busy database, lease loss and termination of child execution.

Evidence: docs/research/2026-11-02-queue-renewal-boundaries.md.

## Reliability defaults and queue heartbeat (approved 2026-11-02)

Shared canonical contract: [reliability-defaults](reliability-defaults.md).
The complete guide is included in this owner's maintenance packet.
