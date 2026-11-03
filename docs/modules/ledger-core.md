# Ledger model and caller-owned storage

Owns scripts/llm_wiki/ledger_core.py: the five existing value classes, record
normalization, explicit connection/path SQL operations, event matching, counts,
reconciliation, recurrence and deterministic entity-page text. It imports stdlib
only. It never chooses a runtime root, opens an active-generation catalog or
publishes a Markdown transaction. The caller supplies its connection or path;
existing commit, connection and error behavior stays unchanged.

scripts/ledger.py retains the nine runtime functions and MAX_PAGES_PER_RUN,
plus explicit identical-object aliases for all moved definitions/constants.
Existing imports keep working. Those aliases remain necessary while fact_keys,
search integrations and other callers use the public ledger surface; remove them
only after a separately reviewed compatibility migration proves no supported
consumer needs them. There is one implementation of each rule, no reverse import
and no dynamic fallback. benchmark/ledger_offline.py consumes the core directly.

Preserve every field, SQL statement, digest, normalization rule, bound, event
weight, transitive grouping, tie/order behavior, missing-table None, recurrence
threshold and Markdown byte. Classes are now defined in the core; legacy ledger
attributes reference the same types and old module-qualified pickle loads are
checked. Newly produced Python pickles name the core module and are not claimed
byte-identical to old pickles; no stored ledger-pickle consumer was found. No
__module__ override is used. SQL/Markdown/JSON representations stay unchanged.

Ten pure tests move with their fixtures/helpers; three real fact_keys.Turn tests
stay in the original integration file. The nine-record regression and four-case
canonical-word regression also move. All original assertions and parameters stay;
the canonical-word test imports the real core instead of the facade. Run the owner
tests and ledger, entity-page, generation, native-frame, cut-disclosure and offline
ledger integrations. Alias/pickle compatibility remains an explicit integration
check; it cannot hide the facade's dependencies inside this isolated packet.

Complete source, own tests, full September22 ledger and September27 cut-disclosure
history, normal bootstrap and instructions must fit <=100000 o200k_base tokens
and reconstruct exactly. Historical statistical/deployment claims are preserved,
not newly validated. No dependency, model call, database migration or live install.

## Qualification research — 2026-11-03

Fresh built-in index9f7124c5, get_architecture/callers and full source searches
identify the ledger facade, fact keys, generation builders and benchmark consumers.
Baseline60related cases pass; actualCCN/shape of the original ledger passes.
Keeping core operations together preserves one ledger model; splitting individual
helpers adds plumbing, while retaining runtime imports prevents consumer isolation.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[SQLite semantics](https://docs.python.org/3.14/library/sqlite3.html),
[pickle module identity](https://docs.python.org/3.14/library/pickle.html).

## Runtime publication default provenance

The facade retains extend_entity_pages(limit=20) for the approved behavior-preserving
contract; callers can already supply another limit. No measured justification for
20 was found. The September27 inventory marked this constant not worked, and the
old contiguous block let an unrelated neighbour comment satisfy the presence guard.
Separating the facade exposed that gap. The constant now states compatibility and
the need for separate workload-based recalibration explicitly; neither the value
nor the guard changes. This is documentation of an existing uncalibrated default,
not a claim that 20 is an optimal throughput or safety limit.
