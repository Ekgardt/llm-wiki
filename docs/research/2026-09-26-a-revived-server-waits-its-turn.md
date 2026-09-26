# A revived server waits its turn

Date: 2026-09-26. Audit 2026-09-26, finding C-2 (B-40 incomplete).

## What was wrong

Facts, from `scripts/pyright_session.py` at a2965838:

- A session whose process reached `FAILED` handed it to cleanup and cleared
  `_startup_attempted` (`_retire_failed_process_locked`), but set no
  `_startup_retry_after`. The retained failed process also made
  `_startup_claim_refused_locked` answer "never refused", so the next query
  cleared the dead process and launched a replacement at once — no pause.
- `_startup_retries` was set to 0 only in `__init__`. A session that had spent
  its three retries, then ran for days, could never be revived again.
- A process reaches `FAILED` only with `process_exited`, `heartbeat_failed`,
  `restart_failed` or `startup_failed` (`lsp_process._select_terminal_failure_locked`
  callers). It is already the second failure: `lsp_process` restarts once on its
  own first. Identity, protocol and capability failures are classified where a
  start fails (`_startup_is_retryable`): a replacement that fails for one of
  those schedules no further retry.

## Decision

- Reviving a failed process spends one retry and sets the backoff
  (5 s, 30 s, 120 s) the way a failed start does. Clearing what the dead process
  left still runs at once; only the launch waits (`_ready_to_launch`,
  `_paced_by_backoff`), and the query answers degraded with
  `<profile>_startup_retry_pending` meanwhile.
- A process that ran `HEALTHY_RUN_SECONDS` (600 s) before failing gives the
  session its retry budget back, the rule Kubernetes uses.
- Guard: `tests/test_a_failed_server_is_started_again.py::test_every_counted_retry_is_paced`
  parses `pyright_session.py` and fails if any function that spends a retry
  (`_startup_retries += 1`) does not also set `_startup_retry_after`.

## Source

Kubernetes documentation, Pod Lifecycle, fetched 2026-09-26 from
https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/workloads/pods/pod-lifecycle.md
(the source of https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/):

- "After containers in a Pod exit, the kubelet restarts them with an exponential
  backoff delay (10s, 20s, 40s, …), that is capped at 300 seconds (5 minutes). Once
  a container has executed for 10 minutes without any problems, the kubelet resets
  the restart backoff timer for that container."
- "**Backoff reset**: If a container runs successfully for a certain duration
  (e.g., 10 minutes), Kubernetes resets the backoff delay, treating any new crash
  as the first one."

## Files

- `scripts/pyright_session.py`
- `tests/test_a_failed_server_is_started_again.py`
- `docs/CODE-NAVIGATION.md`
