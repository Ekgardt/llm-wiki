# One rule decides which queue a reader opens

Date: 2026-08-27.
Question: Reliability V3 adoption replaced `run/queue.sqlite3` with a JSON
tombstone. `markdown_transaction.active_or_legacy_coordinator` is the single
place that decides which coordinator a writer gets; the queue had no such place,
so all seven readers outside `scripts/memory_queue.py` constructed
`MemoryQueue(state_root)` themselves and every one of them opened the tombstone.
Before adding the queue's half: is one routing function the right shape at a
cutover, what should the retired path say when something still opens it, and
what does a façade like this legitimately hide?

## Finding 1 — the choice belongs in one façade, not at each call site

This is the routing half of the strangler fig pattern, and every current
statement of it puts the decision in a single interception point rather than in
the callers. Microsoft's Azure Architecture Center describes a façade that
"intercepts requests going to the legacy system" and routes each to the legacy
application or the new service; AWS Prescriptive Guidance says the same, and
the practitioner write-ups are explicit that the switch is *upstream* of the
callers — a proxy, gateway, or anti-corruption layer — precisely so the
condition is not restated everywhere.

Measured here, restating it everywhere is exactly what happened. The code graph
gives `MemoryQueue` 41 callers. Inside the module `_queue()` feeds fourteen —
the whole operator CLI, the worker, `drain_with`. Outside it there were seven
direct constructions: `doctor.py` 7329 and 7378, `archive_daily.py` 158,
`compile_memory.py` 3918 and 3929, `mcp_server.py` 2825 and 2852. None of the
seven consulted adoption, and on this vault all seven raised
`sqlite3.DatabaseError: file is not a database`. Two more paths did the same
thing by naming the legacy path directly rather than by constructing the class:
`mcp_server._queue_database_path` and `memory_queue._open_queue_ownership_db`.
Nine places, one question, nine answers — the shape the pattern exists to
prevent.

## Finding 2 — a cutover façade is transitional, and this one is not

The dual-read literature is unanimous that the routing layer has an end date:
you run old-and-new for a while, then "cutover is the point where your
application stops reading from the old structure … removing that dual-read
logic". That is the part this vault cannot copy. Adoption is one-way per vault
but the product ships to vaults that have not adopted, and to fresh installs
that never will until they run the command, so the two-armed rule is permanent
product code rather than scaffolding. The consequence worth naming: nothing will
ever delete this branch, so it must be cheap and it must be one function — the
usual justification for tolerating a long-lived façade ("temporary") does not
apply and cannot be used to excuse a second copy of the condition.

## Finding 3 — follow the tombstone with the reader that already exists

Cassandra practice warns that tombstones replicated forward "poison" the new
cluster — reads do extra work for records that are only markers. The analogue
here is a second parser: a marker that every reader interprets its own way is a
marker that will eventually be interpreted two ways. `installed_memory_repair.
adopted_database_path` already reads the tombstone, checks that it names *this*
database, its own legacy path and its own replacement, and returns the legacy
path unchanged for anything that is not a valid tombstone for that database. The
refusal added today therefore does no parsing: it checks the SQLite magic
header, and only when the bytes are not a database does it ask that one reader
where the queue lives and report the disagreement. A second tombstone parser in
`memory_queue.py` would have been the poisoning.

## Finding 4 — the retired path has to name itself

Measured before the change, on this vault:
`uv run python scripts/memory_queue.py status` printed `{"codes":
["sqlite_error"]}` and exited 2. That names neither the boundary that broke nor
the path that broke it, and `work` — step one of the nightly pass — printed the
same. The underlying exception, `file is not a database`, is SQLite reporting a
header it did not recognise; it cannot know that the file is a deliberate
marker left by a migration that also recorded where the data went. Migration
practice puts the forward pointer in the marker for exactly this reason, and the
marker here does carry `replacement_path`. So the refusal now carries the code
`queue_tombstoned_by_adoption` and a detail naming both paths, and the CLI
prints a redacted, bounded `detail` alongside the code.

## Finding 5 — a façade cannot hide a narrower backend, and should not try

The routing guidance assumes the two arms answer the same interface; that
assumption is false here and pretending otherwise would be the dangerous part.
Measured by comparing the two classes: the adopted backend implements 204
methods but is missing fourteen public ones the legacy queue has, and four more
have narrower signatures. Where the gap is mechanical — `get`, `list_tasks`,
`count_eligible`, the source-failure trio, `retains_run_directory`, `restore` —
it can be closed honestly against the same v3 schema. Where it is not — the
source-fence family, retry-policy overrides, `include_dead` purges — the right
answer from the migration literature is the same as its answer to a partly
migrated read: fail, loudly, at the boundary. Silently ignoring an argument the
new backend cannot honour is the "poisoned read" of an API: the caller believes
a fence was taken, or a policy applied, and nothing says otherwise.

## What this changes

- One function, `memory_queue.active_or_legacy_memory_queue`, mirroring the
  coordinator's rule in name and shape and reading the same evidence.
- The legacy path refuses by name when it is a tombstone, using the one reader
  that already follows tombstones.
- Behaviour the adopted backend does not implement is refused by name
  (`queue_api_not_adopted`), never ignored.

## Sources

- [Strangler Fig Pattern — Azure Architecture Center](https://learn.microsoft.com/en-us/azure/architecture/patterns/strangler-fig)
- [Strangler fig pattern — AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/strangler-fig.html)
- [Database Migration Cutover: Making a Clean Break](https://cicd.ariefw.com/articles/21-5-when-your-database-migration-needs-a-clean-break-the-cutover-phase/)
- [Zero-Downtime Database Migration: Cutover Cheatsheet](https://codenicely.in/blog/businesses/saas/database-migration-zero-downtime-cutover-patterns)
- [Cassandra Migration | Cutover & Validation — JusDB](https://www.jusdb.com/databases/cassandra/migration)
