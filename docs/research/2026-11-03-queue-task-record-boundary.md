# Queue task record boundary

Date: 2026-11-03. Under the approved autonomous decomposition mandate, move record
construction/serialization from memory_queue into llm_wiki.queue_task_records.
This is a representation boundary, not another queue or transaction owner.

Exact symbols: _task_record, _export_task_record, _exported_lease,
_exported_result, _exported_capture_binding, _exported_attempt_history,
_exported_source_links, _exported_task_record and frozen _ExportedRows.
Leave _read_exported_rows and every SQL statement, payload hash check, lease,
transaction, state transition, result/artifact operation and ordering at the queue.
Legacy _task_from_row and v3 _v3_task_record still decode their respective payload
columns and call the same row constructor. The new owner imports only existing
queue_contracts, queue_timing and formats.schema plus stdlib.

The four root names called by remaining queue code remain direct aliases:
_task_record, _export_task_record, _exported_task_record and _ExportedRows.
The class keeps its old module identity through that required alias. The five
internal export-field helpers have no outside consumers and no duplicate root
bodies/unused aliases. Public QueueTask and AttemptRecord aliases remain unchanged.
Remove an alias only after its demonstrated queue/serialization consumer is gone.

Production body transfer is exact except the schema resource expression: after
moving one directory down, the v3 serializer names the same scripts/schemas/
queue-task-v3.json from its package parent. Validate that actual path and preserve
the schema bytes; no schema migration, format rewrite or new package is involved.
The schema becomes this record owner's tracked resource. Generic schema validation
continues to be the existing closed subset, not a claim of full JSON Schema support.

Three independent primary sources checked 2026-11-03:
- Python's project-supported dataclasses API preserves explicit field order and
  frozen records: https://docs.python.org/3.10/library/dataclasses.html
- SQLite SELECT/ORDER BY semantics require keeping row selection and ordering at
  their current SQL boundary: https://www.sqlite.org/lang_select.html
- The schema's named 2020-12 vocabulary specifies validation assertions:
  https://json-schema.org/draft/2020-12/json-schema-validation
The latter is the project's pinned vocabulary document (an expired IETF draft,
not a new final RFC). Python 3.10 is upstream-unsupported but remains the required
product compatibility target. No technology/version upgrade is proposed.

Alternatives rejected: moving database reads into a formatting helper obscures
transaction ownership; duplicating the mapper creates two field contracts; a new
queue façade or DI framework is unnecessary for these pure functions. A file-size
split without the shared record responsibility would not remove executor coupling.
All root consumers were searched, including private helpers and monkeypatches;
none patches these field helpers or the queue's schema validator. Built-in caller
queries and current source complement the incomplete repository graph.

Qualification: preserve baseline original source and AST; exercise old functions
on real queued rows before transfer and retain exact expected records as fixtures.
Add a missing-owner import/boundary regression before moving code. Compare field
and timestamp behavior, absent/partial lease/result/capture binding, history order,
actual schema refusals and immutable record construction. Keep all existing queue,
migration/purge and corruption tests untouched and rerun them, including the real
v3 export/schema round-trip. Run Python 3.10, actual CCN/shape, full packet closure,
whole-project ownership and emitted bundle reconstruction. No live queue is touched.

## Implemented candidate and evidence

Fresh generation-18e475e17530a374-7cdc04e6 matches d71e4424. It resolves two
_task_record callers and one each for _export_task_record, _exported_task_record
and _read_exported_rows; graph completeness remains false. Source inspection
confirmed the actual legacy/v3 get/export consumers and absence of helper patches.

Baseline queue/migration/corruption: 172 passed. Before extraction, five real
candidate-queue states (ready, leased, published, succeeded, cancelled) supplied
retained row/task/v3/legacy output fixtures, including Unicode payload and source
links. They contain only generated test data. The absent owner failed collection;
all four separate boundary guards failed before transfer. Initial implementation
passed sixteen cases and failed the guarded import because its explicit allowlist
omitted queue_timing's existing reliability_defaults dependency. Naming that real
lower dependency fixed the guard; no executor or broad package exemption was added.

Eight function bodies and the frozen class moved; seven functions and the class
retain exact AST, while the v3 export changes only its same-resource path expression.
All other 314 top-level queue function/class ASTs, including SQL readers and methods,
are unchanged. The original schema digest is unchanged. Removed the already-unused
NamedTuple import and excess top-level blank lines with whole-AST equivalence checks;
no embedded SQL/string text was normalized. Private alias identity and row pickle
lookup through memory_queue are verified in the existing integration test file.

New owner plus unchanged queue/migration/corruption consumers: 189 passed on Python
3.12 and 189 on Python 3.10. Alias/owner checks: 23 passed. Global modules/policy:
1754 passed / 2 skips in 154.47 seconds. Ruff and actual CCN/shape pass. All 70 emitted
bundles reconstruct original entries; the initial task-record packet is 81785 tokens,
with a 96478-token maximum. Final documentation is remeasured before commit.
Ownership is still incomplete: 567 / 3286 tracked files, 2719 unassigned. The large
queue façade and integration files are not declared complete by this lower boundary.

Private evidence: queue-task-records-{baseline,red,boundary-red,green,consumers-green,
consumers-py310,aliases,global,bundles}.txt, original source/hashes, transfer JSON,
actual fixture and ownership report. Full 725e44ba is still a separate prior-revision
run. No native Windows/macOS, live deployment or useful LLM-cycle saving is claimed.
