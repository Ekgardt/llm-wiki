# Breadcrumbs survive a busy writer

Research date: 2026-09-30. Applies to the adopted Reliability v3 vault.

## Failure and data path

Prompt and tool hooks previously called append_daily synchronously, with 2.5 s
to acquire the Markdown writer and finish. The parent delegate has 3.5 s and
the host has 5 s. A failure before transaction preparation left no complete
breadcrumb for a worker. Retaining its dedupe identifier did not retain its
bytes. A pending project checkpoint contains derived context, not this record.

The regression holds a real adopted coordinator's writer gate while calling
each shipped hook with its shipped budget. Before this change both return
false with no retained intent. The fix accepts an immutable intent while the
gate is held, and a worker writes its source after the gate is released.
The historical timeout's exact competing process is not established by that
test; the missing durable handoff is established independently.

Graph investigation and source checks covered adapter normalization, prompt/tool
dedupe, the old synchronous append, capture publication and adoption, ownership
and intent fences, queue claims, decisions, Markdown transactions, terminal
records, and retained-source cleanup. Graph coverage is best effort; the
existing schemas and actual runtime failure trail were checked directly.

## Research and choice

- [AWS transactional outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
  describes durable source publication before asynchronous dispatch and requires
  idempotent consumers because delivery can repeat.
- [Microsoft duplicate detection](https://learn.microsoft.com/en-us/azure/service-bus-messaging/duplicate-detection)
  explains the ambiguous-acknowledgment case: a publisher may fail after the
  broker accepted a message. Retry identity must survive this ambiguity.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) explains the
  rollback journal's durability and synchronization principles. Its rollback-specific
  algorithm is not the installed WAL algorithm; local databases already use WAL/FULL.
  This change introduces neither a journal-mode migration nor another database.

These sources support the principles, not a claim that they prove this local
implementation. The fault tests supply that evidence.

Chosen: extend the already approved v3 capture producer/consumer path. Increasing
the synchronous wait conflicts with the host's timeout. Writing to disposable
logs loses recovery authority. A second outbox, daemon, or new database duplicates
the installed capture-intent mechanism. LLM classification of an exact breadcrumb
would add cost and could discard or alter an event that needs no classification.

## Contract

The existing filters, redaction, previews and content rate limits run first.
The adopted hook writes a create-only, canonical capture-intent/v1 with event
user_prompt or post_tool_use into the existing pending/ready directories.
Publication uses the same canonical ownership and intent fences, hash-bound
queue links and adoption protocol as session capture. It does not take the
global Markdown writer gate. A failed dispatch is called deferred only after
the producer re-reads and validates the exact retained source. Failure before
retention remains a failure, never a successful acknowledgment.

For breadcrumbs, the existing operation ID names the occurrence. Its first
publication fixes the timestamp and content. A retry reuses those immutable
bytes under the publication fence; different content under the same ID is
rejected. Content digests still validate the entire source. SessionEnd and
PreCompact intent identities and existing provider decisions are unchanged.

A worker creates a capture-breadcrumb-decision/v1, containing the exact append
plan and a named deterministic renderer. It has no provider or model response.
The common queue decision sealing, captured Markdown transaction, terminal
publication, retries and redrives remain authoritative. Loading a saved decision
recomputes its plan from the source and rejects any discrepancy. Worker time
does not redate the entry. Breadcrumbs do not masquerade as full session records.

The change adds no runtime root, database, daemon, environment variable, rate
limit or retention limit. Existing host/delegate limits and operational database
admission remain: unavailable storage may prevent initial acceptance. The
guarantee is durable recovery after acceptance, not guaranteed acceptance under
every storage failure. A surviving intent can be processed on a later worker or
maintenance pass; a successful process spawn is not a completion proof.

## Compatibility and cleanup

Already completed synchronous appends are recognized through their retained
transaction and actual daily operation marker, preventing an upgrade replay from
duplicating the entry. Unadopted v2 installations retain their supported synchronous
writer until adoption; it is not used by an adopted vault. Remove that compatibility
path only when support for unadopted callers is explicitly retired and their
migration is verified. The old daily writer still serves other legitimate callers.

The old provider-decision reader is required by retained session intents and
redrives. The new reader is required once new breadcrumb intents exist: reverting
to older code cannot process that event type. Keep the new reader throughout the
retention period and any rollback migration. No retained source, old failure trail,
transaction witness or undo artifact is deleted to make health appear green.

The old state-lock fallback also changed an identified event's operation ID by
adding `fallback:`. After state recovery, the same event acquired a different
ID. A regression reproduces that difference. Identified events now use the same
ID on both paths; the duplicate hash implementation is removed. Already written
legacy fallback markers remain recognizable during upgrade. Unidentified legacy
calls still receive fresh IDs because no upstream identity exists to recover.

## Checks

Tests cover both shipped hooks with a held writer gate, replay after midnight,
immutable payload mismatch, invalid renderer output, a dispatch exception,
failure before any source is saved, a real child-process exit after ready-file
publication, recovery after its lease expires with the real process-death probe,
reuse of a published decision after an interrupted worker, and an already
completed synchronous capture across the upgrade. The expired-lease test advances
the registry's test clock; it does not relax the lease or liveness requirement.

Related provider-capture, queue-link, adoption, schema, redrive and compatibility
tests remain required. Local Lizard and AST checks enforce CCN <= 5, at most two
if statements per changed function, and at most two levels of conditional/loop
nesting. Installation and the live acceptance result are recorded privately in
the existing audit report rather than inferred from CI.


## Follow-up: retain before opening operational databases (2026-09-30)

The first fix still opened the validated coordinator before saving the intent.
The delegate can terminate at 3.5 seconds, while admission may retry for 30 seconds;
one real cold queue/coordinator opening took 0.955 seconds. A child-process exit
at that boundary reproduces an empty pending directory on the previous version.
A retained operation identifier alone cannot recover the missing source bytes.

The hook now creates and synchronizes the same immutable pending file before any
operational database opening, then wakes the existing worker. The worker discovers
unindexed pending files and runs the unchanged validated ownership, intent-fence,
hash-bound publication and queue protocol. Fresh unindexed ingress is immediately
eligible; interrupted indexed publishers retain their existing stale-owner rules.
Invalid coordinator state still prevents publication, but does not discard input.
A concurrent producer must validate and synchronize the retained canonical record
before acknowledging it. Changed bytes under the same occurrence remain an error.

Additional primary sources fetched for this decision:
- [Transactional outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html):
  durable messages precede relay and consumers must handle repeated delivery.
- [PostgreSQL WAL introduction](https://www.postgresql.org/docs/current/wal-intro.html):
  recoverable durable evidence precedes the state change it supports. This is a
  durability principle, not a proposal to introduce PostgreSQL.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): file and directory
  synchronization are part of a durable acknowledgment, not merely a successful write.

A longer host timeout does not remove the durability gap. Skipping database
validation would weaken publication. A second outbox, lock, or daemon is unnecessary:
the existing pending files and fenced relay already provide the required boundary.
The replaced synchronous breadcrumb publisher and its unused payload callback are
removed. Session capture, saved ready/indexed-pending records, deterministic worker
rendering, and the model contract remain compatible.

Backup already snapshots capture-intents independently of its owner fence, by the
owner's September 27 decision. An unindexed pending record is included byte-for-byte;
the existing filesystem-based deletion proof reports capture_intent_retained even
without a queue row. Worker sweeps use their supplied queue's state root, preventing
an isolated or restored queue from accidentally scanning another runtime.

Guards cover death before database opening followed by real worker completion,
two concurrent producers, unchanged first timestamp, tampered input, an invalid
unindexed head with valid work behind it, legacy completed receipts, held Markdown
writer, backup retention and deletion refusal. Historical failed hook occurrences
are not retroactively declared delivered by installing this repair.


## Publication races retain their real outcome (2026-09-30)

After the durable-ingress repair, four actual adoption diagnostics reported
PermissionError/outside-state-root. Each named intent has a ready file whose hash
matches its markdown_committed terminal. The common bounded reader wrapped lstat
ENOENT together with parent containment failures; a pending file removed by the
other successful publisher was therefore mislabeled as a path-boundary violation.
Two deterministic races reproduce that exact error: publication finishes during
pending discovery, or between discovery and reading the saved descriptor.

Parent containment validation stays unchanged. The final lstat now preserves its
actual missing-file error, allowing discovery to observe a vanished entry normally.
For an already discovered descriptor, the pending relay can use the corresponding
ready file only after validating it against the same saved hash and byte size;
missing or changed ready bytes still refuse publication. It then uses the existing
idempotent fenced publisher. No diagnostic is suppressed solely because its ID
was seen before, and no new wait, path, or weaker permission check is introduced.

Graph edges for generic Path.resolve calls include unrelated resolver methods;
the actual reader, publisher and recovery source were checked directly. Existing
readers, backup and operational-database clients retain the boundary/ownership/
no-follow checks. Guards cover the two real race points, missing both files,
changed ready bytes, and missing files inside versus outside the configured root.
The older four diagnostic records remain history; their individually verified
ready/terminal proofs distinguish successful delivery from a genuine missing input.
