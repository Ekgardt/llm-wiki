# Queue evidence representation and one attempt-history mapper

Date: 2026-11-03. Two ordered refinements under the approved decomposition mandate:
first reuse the identical attempt-history representation; then isolate immutable
queue evidence records. Neither change moves SQL, permissions or transaction fences.

## Shared history representation

memory_queue._attempt_history_entry and the dictionary in
queue_task_records._exported_attempt_history have the same five fields, conversions
and insertion order. Confirm their dictionary AST equality before changing them.
Move the existing single-row helper into queue_task_records and make the list mapper
call it. The queue retains a direct alias because corrupt-evidence history calls it.
The legacy QueueTask export's timestamp conversion is different and stays separate.
This removes an observed duplicate format, not a reproduced runtime outage. Existing
captured queue fixtures and actual corruption/export consumers must stay identical.

## Evidence owner

Move to llm_wiki.queue_evidence_records the frozen _CorruptEvidence and nine functions:
_corrupt_metadata, _corrupt_disposition_key, _corrupt_records_match,
_corrupt_manifest, _corrupt_disposition_record, _capture_resolution_record,
_purge_receipt_record, _capture_seal_digest and _require_identical_seal.
These construct canonical evidence bytes, schema-validated records and their exact
identity checks. They acquire no authority and perform no database/filesystem writes.
Every moved name has a demonstrated queue caller, so direct aliases remain there;
_CorruptEvidence keeps its historical module identity through that alias.

Leave package reads, directory/permission validation, raw payload verification,
lineage paging, corruption detection, resolutions, sealing, quarantine, purge,
transactions and operator authorization in the queue. In particular the byte/record
identity helper is not a complete trust decision: its caller must still validate
schema, permissions and actual payload files. Do not move _source_fence_row_matches:
it proves live process ownership rather than formatting evidence.

Four unchanged resources belong to the new owner: corrupt-task-manifest-v1.json,
corrupt-task-disposition-v1.json, capture-task-link-resolution-v1.json and
corrupt-purge-v1.json under scripts/schemas. Replace only the former root-relative
resource expressions with the equivalent package-parent location. Dependencies are
existing formats.canonical/schema, queue_contracts and queue_timing (including its
reliability_defaults dependency). There is no new store, schema, coordinator, runtime
root, event bus, provider or package. No permission check or limit is relaxed.

## Research and tradeoffs

Three independent primary publishers checked today, for these exact contracts:
- Python 3.10 dataclasses field order/frozen semantics:
  https://docs.python.org/3.10/library/dataclasses.html
- SQLite row selection/order and the existing caller-owned SQL boundary:
  https://www.sqlite.org/lang_select.html
- The named JSON Schema 2020-12 validation vocabulary:
  https://json-schema.org/draft/2020-12/json-schema-validation
The last is the pinned vocabulary's draft document, not a new final RFC; the code
continues to implement its existing closed subset. Python 3.10 remains the required
compatibility target despite upstream end of support. No version upgrade is proposed.

Alternatives rejected: putting state/SQL operations beside formatting would obscure
ownership; a generic record factory would hide each concrete schema's field contract;
leaving two attempt mappers perpetuates format drift risk. Existing root aliases are
required by actual queue methods and can retire only after those callers migrate.
The representation owner is not proof that any evidence was authorized or persisted.

Before code: fresh built-in index/callers, original AST/schema hashes, complete reads
of moved bodies and actual consumer tests. Capture old record/digest outputs, prove
the duplicated dictionary, and add missing-owner/duplicate-boundary regressions.
Keep all existing capture-link, corruption/purge, queue and migration assertions;
run real consumers on Python 3.12/3.10 plus schema/tamper/identity/refusal tests, actual
CCN/shape, global ownership and complete <=100000-token packet reconstruction.
No live queue or installation is touched. Full project completion remains separate.

## Qualification evidence

The fresh built-in graph generation generation-18e47694ef1dae13-92dfc7c9
binds checkout 10b3e657. Relevant callers were confirmed through the built-in
Python graph API after the MCP worker reported saturation; the graph remains
partial and source inspection supplies the missing detail.

The old attempt dictionaries are AST-identical. A new duplicate-boundary test
failed before reuse; it now checks a single shared row mapper. Four new evidence
boundary tests failed before the owner existed. All ten transferred definitions
retain their ASTs after reversing only the four equivalent schema-path expressions;
303 remaining top-level definitions, including complete queue classes, are unchanged.
Four schema files retain their original hashes.

Nine fixtures capture original outputs during 57 existing capture-link, corruption
and migration tests, which passed both before and during capture. Their UUIDs and
row tokens belong to disposable test databases, never a live queue. Both Python
3.12 and 3.10 pass 237 owner/alias and existing queue integration tests after the
transfer. Negative checks cover schema refusal, both byte hashes, all task/package
names and each seal identity field. Frozen record and pickle aliases remain valid.
The first complexity run found CCN 7 in the newly added duplication guard; splitting
its AST-call inspection into a helper brings each function to <=5 without dropping
assertions. All changed production and test functions pass actual CCN/shape checks.

All 71 complete packet artifacts reconstruct every path and byte; the largest is
formats at 96577 tokens before this evidence paragraph. This is packet-size evidence,
not a measured full-cycle LLM cost improvement. Native Windows/macOS CI is still
pending; no installation or native-pass claim follows from Linux checks.

Global module, ownership, structure, privacy, limits and complexity gates: 1788
passed, 2 skipped. Final alias/budget/weight checks: 20 passed. Ownership covers
580 of 3295 tracked paths; 2715 remain unassigned, so decomposition is incomplete.
