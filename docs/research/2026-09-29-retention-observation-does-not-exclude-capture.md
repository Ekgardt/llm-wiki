# Retention observation does not exclude capture

Researched 2026-09-29 against the audit candidate based on a5c67aee.

## Observed cause and path

`run_doctor` collects queue/transaction retention findings, then calls
`_run_deletion_check`. That function unconditionally acquired the global
`runtime-deletion-check` owner. `OwnershipRegistry._settle_deletion_check`
therefore refused new capture, project, and Markdown owners throughout the
runtime scan. A recorded PostToolUse append and project checkpoints failed
with `runtime_deletion_check_active`. Prompt/tool breadcrumbs append directly;
changing the exception's classification to "deferred" would not prove that
their input was durable or would be replayed.

The regression fixture recreates the real ownership conflict: a retained queue
result is observed, then a capture or project owner tries to enter during the
runtime validator. The old implementation rejects both with the same exception.
It is an admission failure, not a SQLite import or environment failure.

## Research and alternatives

- [SQLite isolation](https://www.sqlite.org/isolation.html): separate connections
  see committed transactions; rollback-journal readers can still contend with
  writers. This is not a claim that several databases and files form one snapshot.
- [Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/):
  measure instrumentation's effect on critical paths and contention. Here the
  distinguishing measurement is whether a real owner can enter during the scan.
- [Kubernetes probe guidance](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/):
  poorly implemented health checks can themselves cause failures. This is a
  design lesson, not a proposal to add Kubernetes or a new monitoring service.

All three primary sources were fetched on the research date. No dependency,
database mode, public schema, runtime path, or timeout is changed.

Considered: keep the exclusive scan and retry every producer; add a durable
breadcrumb queue; suppress the refusal; or avoid exclusivity when only reporting
retention. Producer retries would still compete with the diagnostic and require
new timing policy. A second queue is unnecessary for this cause. Suppression
would hide a real unsuccessful write.

## Selected behavior and safety boundary

After adoption validation, existing collected retention findings only suggest a
read-only revalidation. The same full runtime validator reads current state,
excluding no owner. Nonempty blockers or unreadable state report nonquiescence
without taking the global owner. Old collected findings are never returned as
new evidence. If fresh validation finds no blocker, the existing exclusive scan
must still establish a coherent quiescent observation. `permit` stays false in
every case; deletion still requires offline action.

Tradeoff: if retention disappears between collection and revalidation, an empty
runtime is scanned twice within the existing shared deadline. Concurrent changes
may conservatively report unknown state. Neither case permits unsafe deletion.
Without collected retention, the existing exclusive path is unchanged.

## Acceptance and cleanup

Regression coverage checks concurrent capture and project admission, disappearance
of the hinted retention followed by an exclusive recheck, and a read failure that
must never claim quiescence. Existing all-role exclusion, adoption, retained-data,
and owner-release tests remain in force without relaxed assertions.

The old unconditional acquisition is replaced; no feature switch or parallel
implementation is retained. This does not reconstruct historical breadcrumbs,
repair Codex trust, or install this candidate on a running vault.
