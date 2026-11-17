# The next queue slice: types and errors of the public contract

2026-11-02. This note cites no owner message, so it records no owner approval. The implementation and checks are described below.
Basis: fec3fbaf, the current built-in index and the AST of the sources.

## The concrete change

Add `scripts/llm_wiki/queue_contracts.py`: one owner of the already existing
task, result and error types. Its own checks live in
`tests/modules/queue_contracts/`; the description and the full context manifest live in
`docs/modules/queue-contracts.md` and `queue-contracts-context.json`.

Exactly 20 definitions move out of memory_queue:

- LeaseFenceError, ResultConflictError, QueueOperationError,
  CompileCompletionError, MigrationBusy;
- QueueFailure, DeferredResult, AttemptRecord, QueueTask, QueueLease,
  CaptureTaskBinding, SemanticDecision, CorruptExportProgress,
  CorruptPurgeProgress, SourceFence, WorkerSummary, PurgeReceipt,
  DeadCaptureRedrive, RestoreReceipt, LeaseProcessor.

Their bodies take 1 025 o200k_base tokens without decorators/imports. Their dependencies are
the standard library. Fields, defaults, inheritance, frozen properties, methods and
signatures are kept. The old public names in memory_queue refer to the same
objects; no wrappers and no second implementation are added. Current consumers
switch to the type owner directly. QueueRetryPolicy with DEFAULTS and TaskFence
with OwnerLease stay with their current owners: their dependencies are not hidden
behind copies of values or substituted annotations.

## Why before a larger split

memory_queue takes 135 934 tokens without tests. The largest classes are the adopted
reader/store (51 120 tokens) and the legacy MemoryQueue (13 470). Splitting
their methods mechanically by length would leave shared dependencies and a risk of cycles. The shared types are needed
by the future store, worker and CLI, but must be available without loading them.

Today compile_controller imports QueueFailure, and markdown_transaction imports
CaptureTaskBinding, through the queue executor. Four more consumers mix
imports of types and operations. This is the observed reason to extract the contract, not
a promise of significant file savings from moving 20 small definitions.
This step does not change the work of the store, leases, SQL, migrations, subprocess or runtime.

## Compatibility and verification

1. Record the decision in knowledge/notes and update docs/STRUCTURE before the code.
2. Pin trusted pickle samples of the original types and errors; keep
   support for the old module/name through re-exports. Check the old samples,
   new round-trips and a real spawn transition. No unpickling of external data is added.
3. Compare the AST of all definitions and the identity of objects across two import paths.
4. Before the move, reproduce the type's dependency on the executor; after it, forbid it
   with an automatic check. A fresh process loads only the contract and stdlib.
5. Keep the integration scenarios of the queue, compile completion, capture,
   recovery and processes. The shared complexity check is mandatory.
6. Measure the code, tests, data, instructions, required contracts and docs as a whole;
   accept the package only at ≤100 000 tokens. The source's count is not passed off as the
   size of the whole package. Cross-module scenarios are counted separately.

QueueTask and QueueLease themselves get no new field checks: this is a move of
the existing contract, not a change in data admission. No new limits,
Protocol, DI container, service, runtime directory or DB. Installing the whole
unfinished branch into the live vault is not part of this slice.

## Stable serialization name

During the move, keep `__module__ = "memory_queue"` on these public types:
it is their existing serialization name, and the new file owns the implementation.
Do not introduce a custom Unpickler, a migration table or a second class. The old facade
binds the same name to the same object. Such an explicitly documented public
path is required as long as the existing pickle names are supported, and for the mixed
moment of a source update between an already running parent and a new child process.

Trade-off: importing queue_contracts and creating objects from it do not load the
queue, but pickle may resolve the public name through memory_queue. Full
independence of serialization from this facade is not promised at this stage. Later
the facade should become thin when the executor is split; changing the serialization name
for the sake of a pretty graph is unsafe. Keeping the metadata does not duplicate the implementation.

All 20 original types and five characteristic values are pinned in trusted
local baseline samples of protocol 4; the original round-trips passed. A future
check must additionally confirm that the public module/name is kept, along with the
bytes of the original samples and real transfer of a result through spawn. It is not enough
to check only that an old pickle is read by new code: the reverse direction
and an old parent matter too. Artifacts: logs/queue-contracts-pickle-baseline.json,
logs/queue-contracts-baseline-classes.pickle, logs/queue-contracts-baseline-values.pickle.

## Alternatives and sources

Leaving the types with the executor keeps the reverse dependency. Putting them in formats
mixes canonical wire bytes with operational Python objects. Moving
worker/store at once creates a wider risk before the shared boundary is defined. The chosen option is the
minimal self-contained contract that the further split needs.

Primary sources checked 2026-11-02:
[Bounded Context](https://martinfowler.com/bliki/BoundedContext.html): a consistent
model and explicit relationships; [Import Linter](https://import-linter.readthedocs.io/en/stable/contract_types/):
protection of import directions;
[Python pickle](https://docs.python.org/3.14/library/pickle.html): the dependency of
deserialization on an available module/name, and trust limitations. The study checked the API
on Python 3.10, the project's minimum at the time; since 2026-11-03 the project runs on
Python 3.14 only (`requires-python = ">=3.14,<3.15"`). Unpickling still resolves a class by
its module and name there; 3.14 raised the default protocol to 5 (checked on 3.14.7), which
does not change that resolution. No new dependencies are installed.

Evidence: `logs/queue-contracts-definitions.json`, `logs/queue-contracts-consumers.json`,
`logs/queue-contracts-graph.json`, scripts/memory_queue.py.

## Implementation and evidence

The private decision page is `queue-contracts-decision-20261102`;
STRUCTURE was updated before the move. All 20 class ASTs match fec3fbaf. After
excluding imports and the moved classes, the AST of the remaining queue also matches.
Six consumers were switched directly; operations and lazy import sites were kept.

Before the fix, the new guard reproduced all six dependencies on the executor.
After the fix, the guard also checks aliased, local and star imports. The module's own
package passes 49 checks on Python 3.12 and 3.10. The first run on 3.10 found
a quirk of the test: stdlib copy tries an optional Jython import. The forbidden
import now gets an ordinary ModuleNotFoundError: the module is not loaded, and there are
no private permissions or third-party dependencies.

All 20 classes and five values give the same pickle bytes of protocol 4 as before. A real
spawn carries the values there and back. Separately, the unchanged executor fec3fbaf,
unpacked from Git into a temporary directory, read and reproduced the same bytes.
This is evidence that the wire contract is kept; updating live processes on
the installed system was not done in this slice. On Python 3.10, 175 queue and CLI tests
also passed.

The context includes the source, its own tests and fixture, instructions, structure,
design decisions and the manifest. Integration tests are explicitly separated and are not passed off as
the package of an independent module. The temporary copy of the original executor is needed only for
the compatibility check and is not part of the product. The old class implementation was removed;
the public aliases are kept for the sake of the active pickle contract.

Local verification artifacts: `logs/queue-contracts-boundary-red.txt`,
`logs/queue-contracts-ast.txt`, `logs/queue-contracts-own.txt`,
`logs/queue-contracts-python310-own.txt`,
`logs/queue-contracts-python310-integration.txt`,
`logs/queue-contracts-old-executor.txt`, `logs/queue-contracts-complexity.txt`.
These are local evidence; remote CI and installation are not claimed.

Extended local run: **1 264 passed, 7 skipped**, 291.73 s,
Python 3.12.3. It includes the queue, migration/races, capture bindings,
compile completion, archive, Markdown transactions/recovery, MCP, formats,
structure and the shared complexity control. The list:
`logs/queue-contracts-qualified-selection.txt`; the result and JUnit:
`logs/queue-contracts-qualified.txt` / `.xml`. This is a related set, not the full
project regression. An additional final check of CI discovery, complexity
and the three budgets: 73 passed. Ruff for scripts/tests passed.

The memory_queue source takes 135 129 o200k_base tokens after the move:
its further split remains necessary. This slice removes the dependency of
type consumers on the executor; it is not passed off as the final compression of the whole
system or as proof of savings in LLM requests. The shared branch is not installed.
