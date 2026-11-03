# Queue argument and policy validation

llm_wiki.queue_validation owns the existing pure checks for enqueue arguments,
worker retry/heartbeat policy, state filters, lease values, failures and results.
It imports stdlib, queue value contracts, canonical formats and restricted-path
checks, with no executor, IO or clock.
Messages, exception types, validation order and integer/UTF-8 bounds are unchanged.

Legacy enqueue retains its historical unbounded nonempty kind/dedupe strings;
adopted enqueue retains 64-byte kind and 512-byte dedupe bounds. Booleans are not
accepted as bounded integers. Worker heartbeat must be shorter than its positive
lease, and retry bounds stay positive and ordered. The result-operation check
prevents rebinding an existing operation. These are the previous rules, not new
normalization or limits. Redaction, payload encoding and DB admission remain callers.

queue_contracts owns the existing base filter states and attempt ceiling. The base
filter does not enumerate v3 quarantine/purge states. The historical attempt-bound
rationale remains explicitly unmeasured; its value is not changed in this refactor.
All old queue helper names remain identity aliases until supported consumers no
longer use them. Tests distinguish legacy/adopted policy and exercise UTF-8 bytes,
boolean rejection, retry ordering, heartbeat and result-operation conflicts.

Run `uv run pytest tests/modules/queue_validation -q -s`.
See ../research/2026-11-03-queue-attempt-and-validation-boundaries.md.

## Canonical ownership reference

`llm_wiki.queue_attempts` owns attempt-history and lease/failure SQL steps on
caller-supplied connections and timestamps. `llm_wiki.queue_validation` owns pure
argument/policy checks. Transactions, clocks and admission remain in the queue;
shared base filter states and the unchanged attempt bound live in queue_contracts.

The same owner validates capture descriptor/path/identity/fence-field inputs,
publication identity, scan cursors, repair/quarantine fields and consumer names.
Its two lookup constants retain existing wire values; the identical digest-check
entry points share one implementation. Formats/canonical payload limits and
restricted paths are explicit lower dependencies, fully included with their guides.
SQLite Row appears only as an annotation. The owner does not read or write a DB,
read the clock, acquire admission or authorize a mutation on its own. Concrete
TaskFence and semantic-decision orchestration stay with queue call sites.

Pending and published descriptor checks deliberately retain their different
accepted path/integer shapes and errors. No stronger or weaker normalization is
introduced. Private root helper aliases remain for existing queue call sites;
they can be removed only when those callers or the compatibility surface retire.
The two lookup constants had no outside consumers and have no duplicate facade.
See the full evidence in `docs/research/2026-11-03-queue-capture-validation.md`.
