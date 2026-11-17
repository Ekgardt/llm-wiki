# The declarative queue schema: the next module

Status: This note cites no owner message, so it records no owner approval.
Research date: 2026-11-02. Original candidate version: ad59363f.

## Boundary and purpose

Move from memory_queue.py into scripts/llm_wiki/queue_schema.py exactly three
existing sets of definitions: _QUEUE_V3_TABLE_SQL, _QUEUE_V3_INDEX_SQL,
_QUEUE_V3_TRIGGER_SQL, including both += that form the final tuple of triggers.
That is 22 tables, 5 indexes, 35 triggers, 888 lines / 8534 o200k_base tokens.
The AST uses only the builtin tuple and local names; no imports are needed.

The module owns the SQL description of the structure; it does not execute SQL and does not open a DB.
The queue imports these definitions directly. Computing QUEUE_V3_SCHEMA_SHA256,
QUEUE_COMPILE_SCHEMA_SHA256, application_id/user_version, creating MigrationStatement,
checks of the installed schema, migration/repair/adoption and SQL operations stay
in memory_queue. This keeps the owner of the operational policy and rules out a
dependency of the declarative module on reliable_memory and compile_progress.
The existing names in memory_queue keep the identity of the imported tuples.
Deleting the old definition bodies is mandatory; no second copy of the SQL remains.

Change neither the SQL strings, nor the order of elements, nor the generated trigger names,
nor version/checksum, nor constraints/protocols. A new module does not mean a new
database schema. Do not add a migration, runtime root, database, dependency or daemon.
Applying it to the live vault is not part of this candidate move.

## Why this boundary

Keeping the SQL in the queue leaves 888 lines of a declarative contract
together with execution, although it has no dependency on the executor.
Moving the whole migration now would pull into the module the operational contract classes,
the repair procedures and cross-table reconciliation; that is a separate large responsibility.
Moving into SQL files adds file loading, packaging and paths with no benefit for
this task. An ORM, a schema generator or Flyway is not needed: the existing SQL
is already the contract of the installed system. The choice is plain Python tuples,
a single owner, the old application policy and independent checks.

## Current research

Three independent primary sources were checked:

- [SQLite schema table](https://www.sqlite.org/schematab.html): sqlite_schema
  holds the objects and SQL with normalization. So check both the original strings
  byte for byte and the objects actually created; a match of the catalog SQL alone is weaker than
  preserving the original contract.
- [Python sqlite3, version 3.10](https://docs.python.org/3.10/library/sqlite3.html):
  the transaction context and the execute/executescript APIs differ. During the move,
  do not change the execution mechanism or the connections. At the time the project's
  minimum was Python 3.10 and a 3.10 run was mandatory; since 2026-11-03 the project
  runs on Python 3.14 only.
- [Redgate Flyway Validate](https://documentation.red-gate.com/fd/validate-277578898.html),
  updated 30 October 2026: reconciling applied migrations checks names/types/checksums.
  Here this confirms the need to keep both existing digests;
  introducing Flyway or switching to its CRC32 is not proposed.

This is a justification of a local boundary, not a claim of universal superiority.
Actual baseline: Python 3.12.3, SQLite 3.45.1; dependencies are not updated.

## Code research and state

The built-in index ad59363f was confirmed with changes:stale=false. MCP dependencies
did not finish because of busy graph workers. The same built-in code_graph in a new
process read the current generation and found the callers of _queue_v3_statements:
_build_queue_v3_candidate and _queue_v3_trigger_statements. The graph is incomplete;
the sources and AST confirm the complete absence of imports in the moved block.

A search of all scripts/tests found accesses to the SQL sets only in memory_queue.
Both digests take part in compile admission and installation/adoption; their consumers
stay on the old public names. Constructors, validation, trigger
recreation, row counts and repair were included. The source-identity and durable artifact I/O checks
were considered: the first is tied to a MemoryQueue method and migration errors, the second
to filesystem durability/permissions/deadlines. Moving them is not part of the proposal.

The live LLM Wiki installation stays on e95f6a4a; work happens only
in /tmp/llm-wiki-compile-progress. The known nightly/capture problems are not fixed
by this refactoring. The private memory index is current; private knowledge is not published.

## Checks and the future full package

Before the move: migration, capture links, reliability adoption, compile admission:
58 passed, 3.61 s. The hashes of each SQL set and of both combined schema digests are saved
in logs/queue-schema-baseline-contract.json; the graph in logs/queue-schema-graph.json.
Do not duplicate the SQL itself in a fixture: use verifiable baseline digests
and small behaviour scenarios, including the real trigger/constraint prohibitions.

Order: first the private decision and STRUCTURE, then the code.
Own tests/modules/queue_schema, guide docs/modules/queue-schema.md and
manifest docs/modules/queue-schema-context.json. Check:

- The AST and the original definitions match completely; order/count/name/digest are unchanged.
- An import in a fresh interpreter does not load the queue, storage or other dependencies.
- Real creation of all objects in an isolated SQLite, the constraints and the protective triggers.
- A change/deletion of one definition is detected by the guard; old aliases keep identity.
- The old and new base/compile checksums match; the baseline integration and corruption
  tests pass, then the shared complexity/structure/CI checks, Ruff and Python 3.10.
- The full manifest with all own tests/doc/instructions/dependencies is ≤100000 o200k_base,
  without hiding required files. The final size is not measured yet: the module has not been created.

The original block of 8534 tokens is not the size of the full package. If the full package exceeds
100000, do not weaken the budget and do not split by an arbitrary line count.
After the result: measured CI weights, commit/push, refreshing the code index,
the private progress record, and freeing only the task's own test/candidate caches.
Do not claim the full suite, native platform qualification or a live rollout without running them.

Additional baseline: tests/test_queue_v3_corruption.py: 29 passed, 12.01 s;
logs/queue-schema-corruption-baseline.txt. These are checks of the original implementation,
not proof of the move not yet carried out. 


## Implementation of the boundary

The private decision and STRUCTURE were recorded before the code. The SQL block was moved byte for byte,
and the remaining queue AST matched after excluding imports and the moved definitions.
Source: logs/queue-schema-preservation.json. The boundary guard before the move
failed on the old definitions; after the move it passes. The owner has no imports.

18 own checks passed: ordered baseline hashes, a deliberate change
or deletion of a definition, creating all objects in a real SQLite, CHECK/FK,
immutable history and refusal of deletion without authorization, a successful permitted
deletion, a fresh interpreter without runtime imports. There is no duplication of SQL in a fixture.
Initial full package:76964 o200k_base tokens/23 files.
The integration check compares the identity of aliases and both combined fingerprints.


## Qualification of the result

Extended set: 293 passed, 65.40 s: own, migration, capture links,
adoption, compile admission, corruption, memory queue, global complexity/shape,
structure and CI policy. Python 3.10 isolated locked dev environment:
106 passed, 18.25 s: own and the five baseline integration files.
Both combined fingerprints matched ad59363f. Ruff and git diff --check passed.
The memory_queue source shrank from 129165 to 120706 tokens; all 11 packages are below 100000.
This is not the full regression suite, not native Windows/macOS qualification and not
a live rollout. The changes are kept only in the candidate branch.

Evidence: logs/queue-schema-own.txt; logs/queue-schema-related.txt/.xml;
logs/queue-schema-python310.txt; logs/queue-schema-all-budgets.json;
logs/queue-schema-final-budget.txt. The CI weights of the new tests were measured from JUnit;
the final budget includes this report. The SQL was preserved byte for byte, also after Ruff.
