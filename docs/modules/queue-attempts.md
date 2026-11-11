# Queue attempt and lease SQL steps

llm_wiki.queue_attempts reads/writes caller-supplied SQLite connections using
caller-supplied timestamps. It owns attempt-history reads and append operations,
lease compare-and-swap updates, stale/expired lease validation, lease input binding,
exhaustion retirement and failure disposition. It never opens a connection, starts
or commits a transaction, reads the current clock or grants admission.

Callers must retain their original transaction around related steps. In particular,
history and failure state roll back together if the caller aborts. Legacy and v3
claim SQL remain distinct: v3 clears previous error/capability fields; legacy does
not. Capability blocking refunds the attempt before permanent/exhaustion decisions;
other permanent or exhausted failures become dead. A stale token cannot update the
current lease. Retry calculation remains owned by queue_timing and receives now.

Dependencies are queue value contracts, canonical formats, timing and defaults.
The existing permanent-code set has one owner here; old queue names are identical
aliases. The shared attempt ceiling remains the historical value in queue_contracts.
No policy, SQL, schema hash, transaction boundary or public command is changed.
Unit tests use real schema tables and SQLite for rollback, stale tokens, claim races,
input integrity, exact expiry and batched history reads. Integration tests continue
covering the complete queue and ownership pipeline.

Run `uv run pytest tests/modules/queue_attempts -q -s`.
See ../research/2026-11-03-queue-attempt-and-validation-boundaries.md.

## Canonical ownership reference

`llm_wiki.queue_attempts` owns attempt-history and lease/failure SQL steps on
caller-supplied connections and timestamps. `llm_wiki.queue_validation` owns pure
argument/policy checks. Transactions, clocks and admission remain in the queue;
shared base filter states and the unchanged attempt bound live in queue_contracts.
