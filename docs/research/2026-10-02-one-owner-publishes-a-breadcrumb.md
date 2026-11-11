# One capture owner publishes and registers a breadcrumb

Research date: 2026-10-02. Python baseline 3.10. Qualification is Linux only unless separately stated.

The genuine native observer recorded five PostToolUse failures at 5001–5009 ms under the installed five-second host timeout. Two matching occurrences have complete verified ready manifests; missing files for the other computed identities do not independently prove loss. The newer observer confirms the same error digest reports a timeout and records further failures. None is removed from evidence.

Actual installed idempotent publication of the original accepted inputs took 3.8231 and 0.1179 seconds. The first profile spent 3.5515 seconds in ownership acquisition and 3.6092 seconds in SQLite BEGIN IMMEDIATE across the complete call. Both source and generation/model outcomes remain unchanged. This is publication profiling, not native CLI or full-event latency. A ten-second read-only Linux lock observation found concurrent capture-worker and hook processes taking the coordinator and queue locks. A separate worker turn refused owner_busy after 3.8689 seconds, with no processed work or model call. The kernel refused attaching a syscall tracer; its security setting is unchanged.

The existing publication used two sequential capture owners for the same occurrence: one to store the complete bundle, another to register it. One outer owner and intent fence can protect both stages, with the existing heartbeat. Queue registration still follows durable manifest publication. Failed registration retains and verifies the complete evidence for existing recovery.

## Sources and alternatives

- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html): one reserved writer and exclusive commit phases serialize these operations. This remains the supported rollback-journal runtime, not WAL.
- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html): transaction and timeout behavior. No new Python or SQLite feature requirement is introduced.
- [Linux proc_locks](https://man7.org/linux/man-pages/man5/proc_locks.5.html): kernel lock observations bind access mode, PID and inode, not application call stacks.
- [Official OpenAI hook documentation](https://learn.chatgpt.com/docs/hooks#run-hooks-in-the-background): background hooks can finish out of order and are cancelled at session end. Async mode is not installed because preservation before cancellation has not been qualified. The first attempted English documentation URL was unavailable; the localized official route resolved to this canonical page.

Increasing a synchronous host timeout alone leaves duplicate ownership work and delays every tool. Ignoring ownership or accepting an unverified manifest would weaken deletion and evidence guarantees. A new database, WAL, a persistent drainer or unfenced filesystem ingress would change current contracts. The chosen internal composition uses one existing owner, with no changed hook configuration, schema, environment, path, daemon, actor or numerical limit.

Recovery callers without a supplied ownership tuple still acquire their own fence. A supplied tuple is checked against the coordinator's current canonical owner row, matching live intent fence, scope, owner token and epochs before registration writes. A foreign runtime root or forged owner/fence refuses before publishing bytes. The generic ownership and registry contracts remain intact.

This removes redundant acquisitions. It is not a guarantee that a first acquisition completes within five seconds under arbitrary contention. Whole native latency, durable terminal proof, historical recovery and the remaining audit groups require separate qualification. Failed test runs, including a candidate import mistake corrected before installation, remain in the private evidence. The replaced standalone storage ownership phase has no other code consumer and is removed; preimages remain only for rollback.
