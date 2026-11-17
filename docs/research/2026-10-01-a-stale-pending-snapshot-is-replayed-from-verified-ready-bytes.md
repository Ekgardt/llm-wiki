# A stale pending snapshot is replayed from verified ready bytes

Date: 2026-10-01. Compatible recovery correction; no new schema, path, setting or budget.

The installed capture trail recorded missing pending files at 17:17:57 and
17:43:32. For the first, the ready file's digest and length match the queue
binding, and its linked task succeeded. This is a failed recovery observation,
not proof that its durable conversation disappeared. Historical counters remain.

A real regression records a pending descriptor, completes publication through
the existing publisher, then replays that stale descriptor. Old recovery raises
FileNotFoundError although ready bytes and the task survive. A damaged ready-copy
control also fails with the wrong missing-pending error on old code. The initial
run has 2 failed / 2 passed. This reproduces the mechanism; it does not prove the
precise scheduling of either installed incident.

When, and only when, the exact pending path is missing, read the corresponding
existing ready path using the same descriptor's digest, byte length, no-follow
containment and owner checks. Verify the complete source's handler before using
the unchanged fenced, create-only, replay-safe publication sequence. Missing both
copies, damaged ready bytes and pending permission errors remain refusals.
Nothing treats another copy as authoritative merely because its filename exists.
A full v2 breadcrumb case additionally checks anchor/parts unchanged and one
original task link retained; the v1 session path stays supported.

Sources checked 2026-10-01:

- [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html): relays must handle repeated delivery idempotently.
- [SQLite isolation](https://www.sqlite.org/isolation.html): separate connections observe committed states; a materialized query result is not a lock on later file publication.
- [Python 3.10 exceptions](https://docs.python.org/3.10/library/exceptions.html#FileNotFoundError): distinguish missing files from permission and other operating-system failures.

Choice: verify the already defined immutable copy and replay the existing publisher.
Alternatives: suppressing all missing-file errors could hide real loss; accepting
ready bytes without checking the original binding could admit changed evidence;
locking a whole sweep would delay unrelated captures; increasing timeouts cannot
make an old descriptor current. This correction preserves at-least-once semantics
and does not promise exactly-once execution or deletion permission.

The initial 50-test related group passed. Subsequent qualification, actual Lizard,
installed-module proof and source cutover receipts are kept under
logs/audit-2026-10-01-pending-*. No complete current-source suite or Windows success
is implied. The first candidate mutation command used an unavailable relative
venv and wrote nothing; its unchanged-code failure and corrected run are retained.

Final related qualification: 298 passed / 21 skipped, including actual project journal, durable ingress, publication recovery, complete source/branch/CCN guards and existing hook-budget controls. Changed/new functions independently measured with real Lizard: maximum CCN 4. Initial Ruff import-order failure was corrected without changing checks. Later installed/publication evidence is retained separately.
