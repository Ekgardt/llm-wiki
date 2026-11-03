# Formats module

Approved pilot, 2026-11-02. This is the first extracted module, not completion of
repository-wide modularization or evidence of a reduction in real task tokens.

`llm_wiki.formats` owns canonical JSON bytes and SHA-256, the existing closed
JSON-schema validator, source descriptors, and compile receipt v2/v3/v4 syntax and
identity, plus canonical queue payload validation. It uses the Python standard library only. It does not import the
compiler, cache, queue, transaction engine, configuration, or model client.
Schema files remain at `scripts/schemas/`; wire formats and digest algorithms do
not change. A valid receipt is not proof of committed publication. The compiler
and evidence resolver still verify transaction authority separately.

## Interfaces

- `canonical`: `canonical_json_bytes`, `sha256_bytes`.
- `schema`: `SchemaValidationError`, `validate_schema`, `validate_schema_object`,
  `validate_rule` (the existing schema-subset rule evaluator).
- `source`: `SourceDescriptor`, `SourceOccurrenceBounds`.
- `receipts`: `compile_source_identity`, `compile_context_source_identity`,
  `parse_compile_receipt_v2/v3/v4`, `parse_compile_receipt_version`,
  `receipt_source_fields`, `UnsupportedCompileReceiptVersion`, schema paths.

No second implementation remains in the original files. Existing public names in
`reliable_memory`, `compile_cache`, and `compile_memory` re-export the same objects:
production consumers now import the formats owner directly. These public re-exports
remain for external compatibility and identity tests; removal requires an explicit
retirement of that public contract. Compiler-internal
helpers imported from formats are also the same objects, not forwarding wrappers.
Private helpers are implementation details; consumers of parsing use its owning
module. Parser instrumentation patches the namespace where the caller looks up the parser.

The `queue_payload` component owns `PayloadValidation`, `validate_payload_blob`
and their existing byte, depth, string and container limits. Digest-only inspection
remains distinct from JSON parsing. Queue database state, transitions, leases and
repair decisions remain with their existing owners. `memory_queue` re-exports the
same two public objects for compatibility; internal consumers import the owner.

## Context and qualification

`formats-context.json` describes the whole module maintenance packet: every owned
source, test and fixture, both agent contracts, development laws, schemas, shared
test timeout policy, dependency declaration and this interface guide. The packet also includes the
complete canonical structure document, both preceding design studies and the
pilot qualification report; none of those documents is truncated to meet the cap.
New files in owned directories are counted automatically. UTF-8 text is tokenized
with pinned `tiktoken==0.14.0`, `o200k_base`, without special-token interpretation.
The owner's 100,000-token ceiling is an automatic test, not an estimate from bytes.
Other model tokenizers can differ. The tokenizer is a development dependency only.

Run `uv run pytest --confcutdir=tests/modules/formats tests/modules/formats -q -s`.
This standalone run does not load the repository integration fixtures (code-kernel
servers, hook budgets, and model selection); it uses only this module and the test
timeout policy. The ordinary full suite still loads and enforces every repository
guard. Module tests use frozen receipt
bytes and small contract cases, not compiler integration fixtures. The original
integration suites remain required: transactions, historical archive authority,
secondary receipt consumers, compile hardening, native parts, reliable memory and
security invariants. Their context is separate and must be included when changing
those components; this packet does not claim that every end-to-end task fits into
100,000 tokens. The full regression suite and static checks qualify the extraction.

Boundary tests reproduce the original compiler-parser coupling and prevent its
return. The guard also rejects internal imports of moved primitives through the
three compatibility modules, including aliases, local imports and star imports.
Fresh-process import tests forbid execution dependencies; static imports
must be standard-library or relative within the module. CI shard discovery includes
nested module tests and records their measured cost. Golden records preserve
syntax and identities while integration tests retain publication authority checks.

## Isolated formats module (approved 2026-11-02)

The owner approved the bounded formats pilot after the modularity study, not
a fixed 74-module architecture. `scripts/llm_wiki/formats/` owns canonical JSON,
SHA-256, the existing closed JSON-schema validator, source descriptors and
compile receipt parsing. Its `canonical.py`, `schema.py`, `source.py`, and
`receipts.py` depend only on the standard library and each other, never on
compiler, cache, queue, transaction, model or runtime ownership code.
Existing schemas remain in `scripts/schemas/`. Receipt validity alone does not
prove publication; transaction authority stays with its existing owner.

Module tests live in `tests/modules/formats/` and its contract in
`docs/modules/formats.md`. The complete owned context, shared instructions,
fixtures and dependency contracts must fit 100,000 o200k_base tokens. Cross-module
qualification scenarios remain counted separately and must not be removed.
Existing public helper imports can re-export the same objects for compatibility;
there is one implementation and one exception/type identity. Private parser
consumers must import their owning module. No database, wire format, daemon,
installation command or runtime root is added. Implementation and installed
qualification are separate; the working branch is not installed by this decision.

Research: `docs/research/2026-11-02-module-boundaries-validation.md`.
Private decision: `knowledge/notes/isolated-formats-module-decision-20261102.md`.

## Queue payload format (approved 2026-11-02)

`llm_wiki.formats.queue_payload` owns `PayloadValidation`, `validate_payload_blob`,
their pure JSON/hash helpers and the existing four payload limits. It uses only
the standard library and `formats.canonical`. `memory_queue` re-exports its two
public objects unchanged for compatibility and imports its payload byte ceiling.
Repair reads the validator from its owner. SQLite, task transitions, ownership,
worker execution and migrations remain in the queue. The complete formats packet
includes the new source and tests; its existing 100,000-token gate applies.
See `docs/research/2026-11-02-queue-payload-boundary.md`; private approval:
`knowledge/notes/queue-payload-formats-decision-20261102.md`.
