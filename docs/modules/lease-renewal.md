# Lease renewal module

Qualified in its existing location on 2026-11-02. This is an existing independent
module, not an extraction: `scripts/lease_renewal.py`, all consumer imports,
renewal policy, ownership and runtime locations remain unchanged.

## Responsibility and contract

`renew_until_stopped` schedules synchronous renewal callbacks. It returns the
exception that ends renewal or None when stopped. Acquisition, fenced database
writes, heartbeat threads, cleanup and the caller's response to lease loss belong
to callers. This helper cannot establish or prove ownership on its own.

The initial expiry is measured from required `held_since`, not the renewal
thread's start. Successful attempts move the expiry from their start time.
After a transient failure, the next wait is bounded by remaining slack after
`attempt_seconds` and by the existing interval/5 retry period. Permanent errors
are returned immediately; process-control BaseExceptions propagate. The existing
retry divisor is retained, not introduced by this qualification.

`busy_database`, the default transient test, is owned by
[sqlite-deadlines](sqlite-deadlines.md), the only project module imported here.

This is a cooperative loop: it cannot preempt a blocked callback, verify its
claimed duration or validate a database lease. `attempt_seconds` is the caller's
estimate/bound; multiple statements can exceed one SQLite busy wait. Database
expiry and fencing checks remain the safety boundary. No watchdog or new timeout
is added here.

## Maintenance packet and checks

`lease-renewal-context.json` names the complete source, own test file and inline
data, instructions, canonical architecture, module design and relevant history.
The whole files count, including the budget test itself. The user's ceiling is
100,000 o200k_base tokens, measured with the pinned development tokenizer.
The guard checks all direct/local imports and rejects relative product imports;
negative examples verify the guard. This static check is not an OS sandbox or
a claim about arbitrary dynamically executed code.

```
PYTHONPATH=scripts uv run pytest --noconftest tests/test_a_busy_database_is_not_a_lost_lease.py -q -s
```

The ordinary suite retains its global fixtures and checks. The original eight
cases passed before adding qualification checks. New cases cover SQLite codes,
legacy text, real lock contention, pre-set stop and process-control exceptions.
No runtime implementation changes accompany the tests.

## Consumers and integration scope

The indexed graph and AST at 1e3e5d89 find eight renewal call sites across seven
product files: flush_memory, integration_adapter, operational_ownership,
markdown_transaction, doctor, queue_core (two heartbeats) and private_vault_backup.
Doctor and queue also use busy_database. The graph is incomplete in general;
source inspection supplements it. Callback bodies and injected predicates are
not dependencies of the scheduling implementation.

`test_the_worker_does_not_call_a_broken_database_idle.py` checks classifier
agreement across queue/doctor. `test_capture_renewal_boundaries.py`, queue heartbeat,
Markdown transaction, operational ownership and backup tests remain integration
checks under their existing owners. They are not removed or counted as isolated
module tests. Changes to those callers need their context and evidence too.

## History and current references

The dated September research pages describe successive historical states;
statements that every OperationalError is retried are superseded by the current
source and exact-classification tests. They remain historical evidence, not the
current classification contract.

Primary references checked 2026-11-02:
[SQLite result codes](https://www.sqlite.org/rescode.html) explains primary and
extended codes; [Python sqlite3](https://docs.python.org/3.14/library/sqlite3.html) documents
`sqlite_errorcode`;
[Kubernetes client-go leader election](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go)
separates lease duration, renewal deadline and retry period. It is supporting
research, not a claim that this helper implements distributed leader election.

Keeping the existing stdlib-only module avoids moving critical callbacks or
introducing an executor cycle. Moving it solely for naming uniformity adds
migration work without improving isolation; splitting each caller's retry loop
would duplicate the shared policy. Neither alternative is needed for this packet.

Evidence: local logs/lease-renewal-baseline.txt, logs/lease-renewal-graph.json,
logs/lease-renewal-consumers.json and the existing source/tests.

Qualification (2026-11-02, historical; before the move to Python 3.14): 35 own cases passed on both Python 3.12 and 3.10. The related
renewal/capture/heartbeat and global complexity/CI checks passed (86 cases).
Ruff and the full-source import boundary passed. The source bytes match 1e3e5d89.
The project-wide regression is recorded separately; no live installation or
remote-CI result is claimed by this module qualification.

## Existing renewal and bounded-input boundaries (qualified 2026-11-02)

`scripts/lease_renewal.py` owns cooperative renewal scheduling and SQLite
contention classification. Its callers retain acquisition, fenced writes,
thread ownership and loss handling. The source and imports remain in place;
`tests/test_a_busy_database_is_not_a_lost_lease.py` is its own test file.
See `docs/modules/lease-renewal.md` and `lease-renewal-context.json`.

`scripts/bounded_io.py` owns stable byte reads, verified source ranges and their
existing shared byte limits. It does not own publication, transactions or ACL
writes. Its own tests remain `tests/test_bounded_io.py` and
`tests/test_stable_source_ranges.py`; see `docs/modules/bounded-io.md` and
`bounded-io-context.json`. Renewal uses only stdlib; bounded IO also imports the
shared path_ancestry policy. Their manifests count
complete maintenance packets under 100,000 o200k_base tokens. Related integration
tests remain with their existing owners. No runtime or source-path changes occur.
