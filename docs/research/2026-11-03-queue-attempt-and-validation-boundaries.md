# Queue attempt operations and argument validation

2026-11-03; approved completion mandate; baseline 1d7cb29c.

Two boundaries retain the exact function bodies and caller transaction ownership:

llm_wiki.queue_attempts owns _attempt_histories, _record_failed_attempt,
_failure_state, _apply_failure_state, _failure_is_terminal, _retire_exhausted_history,
_retire_exhausted_ready, _take_task_lease, _take_v3_task_lease,
_record_cancelled_attempt, _lease_row_holds, _require_leased_row,
_require_lease_input, _record_acknowledged_attempt and the existing _PERMANENT_CODES.
Connections and timestamps are supplied. The owner never opens a database, reads
current time, admits a worker or commits. History and task changes remain inside
exactly the caller's previous transaction. Legacy and v3 lease SQL stay distinct:
v3 clears previous errors/capability, legacy does not. Blocked failures retain their
attempt refund; exhausted/permanent failures remain dead; stale tokens refuse.

llm_wiki.queue_validation owns the existing pure checks: _require_task_kind,
_require_bounded_int, _require_dedupe_key, _require_enqueue_arguments,
_require_non_empty_string, _require_legacy_enqueue_arguments,
_validate_retry_policy, _check_policy_integer, _check_retry_bounds,
_check_retry_window, _validate_worker_policy, _check_result_bytes,
_check_result_request, _check_result_operation, _is_positive_int,
_require_positive_lease, _valid_heartbeat_interval, _require_known_states,
_require_error_code, _require_positive_task_lease, _require_claim_owner_and_lease.
It retains distinct legacy/adopted argument limits and error messages. Redaction,
payload construction, ownership/clock orchestration and DB access remain callers'
responsibility. The owner imports only stdlib and queue value contracts.

The existing _STATES tuple and _MAX_RUNTIME_ATTEMPTS move to queue_contracts once;
all consumers share aliases. _STATES is the existing base filter, not an exhaustive
list of v3 quarantine/purge schema states. The attempt bound remains exactly 100,
including its existing comment that the historical basis was not measured. This
refactor neither invents a new limit nor claims that historical basis is known.
All old queue helper names remain identity aliases for existing users/tests, not
second implementations. Alias removal requires evidence that supported consumers
no longer need those entry points. No public class, command, DB schema or hash changes.

Research checked 2026-11-03, independent primary publishers:
- https://www.sqlite.org/atomiccommit.html — preserve transaction boundaries;
  an extracted statement must not commit a caller's partially completed operation.
- https://docs.python.org/3.10/library/sqlite3.html — supplied connection semantics,
  real rowcount, parameter binding and caller-managed transactions. No new API or
  Python-version requirement; the project's 3.10 contract remains despite EOL.
- https://martinfowler.com/bliki/BoundedContext.html — ownership by responsibility;
  importing the executor back into either owner would defeat the boundary.
Alternatives: a connection-owning store changes lifetime and admission; unifying
legacy/v3 SQL changes behavior; a new framework adds no needed capability. Selected
existing functions on supplied resources preserve the actual contracts.

Fresh index and graph queries precede code. Direct usage search finds three old
_failure_state tests and no monkeypatch of moved clocks, retry functions or bounds.
The source matrix and graph remain evidence, supplemented by full source inspection.
Baseline queue/race/input/attempt regressions: 143 passed. Evidence is retained in
logs/queue-attempts-* and logs/queue-validation-*.

## Qualification

All 35 extracted function ASTs match the preimage exactly. Remaining queue and
contract ASTs match after excluding moved definitions/constants and imports;
queue schema fingerprints are unchanged. Empty line runs were normalized with
an additional whole-file AST equality assertion.

New boundary tests first failed eight times before extraction. The first related
run passed 200 tests; the independent functional tests passed 41. Expanded queue,
race, input, structure, complexity, privacy, limit and CI checks passed 327 with
one platform skip. Adopted queue, migration, corruption, fencing, cancellation,
ownership and retry scenarios passed 181. Python 3.10 passed all 41 functional
and boundary tests; token counting uses the main environment's existing tiktoken.
Measured weights passed 43 tests; bootstrap and weight guards passed 54. Ruff
passed. All 32 complete context artifacts decode to exactly the original paths
and bytes and fit the unchanged 100000-token ceiling.

This is related qualification, not a new successful whole-project regression.
The previous pinned 446bdafa full run had 15370 passes, 244 skips and one stale
test clock-binding failure, fixed and reproduced in 7cae0538. A new pinned full
run must include that correction and this extraction. No live installation.
