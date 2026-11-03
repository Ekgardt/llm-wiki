# Queue worker value/state boundary

Date: 2026-11-03. Prepared at 1214d765 under the approved decomposition mandate.

Move QueueRetryPolicy, _ProcessorOutcome, _WorkerProgress (including advance),
processor_error_code and its two existing constants from memory_queue to
scripts/llm_wiki/queue_worker_state.py. Preserve every class/function body,
annotation, default, regex and schema-bound size. Dependencies: stdlib plus
queue_contracts and reliability_defaults. Root aliases preserve existing callers
and patch sites. All four definitions retain their memory_queue module identity
for class/function serialization, including private state records.

This owner holds legacy retry configuration values, processor outcome values,
aggregate worker progress and safe error-code conversion. SQL transitions,
adopted retry decisions, worker deadlines, cancellation, sleeping and child
execution remain at current callers. No clocks, filesystem or runtime imports.
The capture failure helper in flush_memory will import processor_error_code
directly from the lower owner, retaining its lazy import and all decision logic;
source inspection found no root-binding monkeypatch consumer for that helper.
No new universal worker framework, callback layer or protocol is introduced.

Callers found in memory_queue and flush_memory; direct regression is
 tests/test_a_dead_task_names_its_reason.py. Keep that integration suite intact
and add focused cases that first run against the original implementation.
All existing defaults are still evaluated once at import. QueueOperationError
continues to supply its own code; arbitrary messages contribute only when they
match the existing code-shaped pattern. Do not expand the security claim to all
possible exception classes or alter the current truncation semantics.

## Current primary research

Checked 2026-11-03, four independent publishers:
- Python pickle resolves classes/functions by importable qualified names.
  Keep aliases and serialization module names, with byte-level baseline proofs.
  https://docs.python.org/3/library/pickle.html#what-can-be-pickled-and-unpickled
- OWASP logging separates useful event classification from sensitive event data.
  Preserve bounded reason codes instead of exposing arbitrary exception text.
  https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- SQLite CHECK constraints enforce stored-row restrictions. The 64-byte
  stored error-code ceiling is tied to existing attempt_history schema, not invented
  by this transfer. No schema or database changes are authorized here.
  https://www.sqlite.org/lang_createtable.html#check_constraints
- Microsoft retry-pattern guidance distinguishes transient failures, retry
  policy and operation semantics. It supports retaining adopted/legacy policy
  decisions in the queue instead of moving decisions with the value record.
  https://learn.microsoft.com/en-us/azure/architecture/patterns/retry

Alternatives: extend globally used queue_contracts with worker-only mutable state
(adds defaults dependency to all contract consumers), leave value logic mixed
with queue I/O, or move worker orchestration wholesale (large coupled dependency
closure). Choose one worker-value owner and no new behavior or framework.

Before code: fresh built-in index/graph, consumer/patch inspection, original
source/pickle baseline, focused baseline and failing ownership guards. After:
all original AST bodies preserved, root consumer tests, actual CCN/shape,
Python 3.10/3.12, complete dependency/guide/test packet <=100000 o200k_base and
lossless round trip. Full-suite checkpoint and native-platform limitations must
be reported separately. No installation in the live vault.

A separate existing byte/character defect was reproduced during investigation:
a generic Unicode exception type yields 111 bytes and the actual v3 failure
transition raises IntegrityError, leaving its task leased. Exact-body transfer
will not conceal or claim to fix it. Follow with the independently qualified
correction in docs/research/2026-11-03-processor-error-code-byte-bound.md.

## Transfer qualification

Fresh built-in generation-18e4704110c7c4fa-bf4fe008 at 281e01b9 and source/caller
inspection preceded code. All four definitions, both constants and remaining
queue bodies are AST-identical. Capture changes only its lazy reason-helper
import. Eight original worker pickle forms, including the progress method, keep
identical bytes. All existing consumer test files remain unchanged.
Combined pure original-owner baseline 31 passed; seven structural guards failed
before the two-owner transfer. Initial related checks 97 passed, final Python
3.10 with packet tests 99 passed. Queue/capture consumers 138 passed. Global
module/context/privacy/structure/complexity/weight guards 1495 passed/two skipped,
150.01 seconds. Ruff and actual CCN/shape passed; one test's initial CCN=6 was
corrected while retaining its explicit function-type assertion.
All 61 artifacts round-trip and fit; initial worker packet 77896 tokens, largest
97848 before final evidence. Registry 479/3214 leaves 2735 unassigned. Full
1214d765 finished 15855/244 with five warnings in 1783.30 seconds; it predates
these transfers. Native CI remains unconfirmed, and live installation unchanged.
The reproduced Unicode error-code defect is still pending the separate fix.
