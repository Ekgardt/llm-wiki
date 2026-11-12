# Breadcrumb record protocol

The existing scripts/breadcrumb_protocol.py owns occurrence identity, anchors,
canonical parts, manifests and complete ordered reconstruction. Its three schemas
remain in scripts/schemas. Paths, formats, callbacks and all function bodies stay
unchanged. Only MAX_CAPTURE_INTENT_BYTES now comes from the stdlib-only
reliability_defaults owner instead of importing memory_state and resolving a live
vault root. The existing 1 MiB bound is per physical record; logical input is not
truncated, and no new part-count limit is introduced.

Validation checks canonical encoding, schema, digest, intent/index binding, exact
byte count, predecessor chain and timezone/day consistency. Reader-scoped reuse
accepts only the canonical RecordParsing type, checks current schema bytes and
active ownership, and returns fresh decoded values. It never replaces external
source, storage or lease verification. ContextVar restoration still runs on error.

The packet includes complete protocol, schemas, original protocol tests, formats
and reliability source dependencies and guides. Runtime storage/adoption, workers,
recovery and historical reader tests remain additional integration qualification.
No filesystem mutation, provider, queue or live state import belongs to this owner;
reading the fixed schema files remains intentional. Tests retain multi-part Unicode,
byte-for-byte reconstruction, tamper and ordering rejection, immutable-parse reuse,
schema drift and mutable return protection.

## Boundary research — 2026-11-03

The approved complete-project plan permits this existing responsibility boundary.
Keeping the state import couples wire syntax to environment resolution; copying
the bound risks writer/reader disagreement. The existing reliability policy leaf
provides the same value without either problem. Separate per-helper owners would
fragment a single linked-record contract. Historical limit rationale remains whole
in the packet, and the schemas and global durable-ingress contract remain authority.

Sources: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[Python import effects](https://docs.python.org/3.14/reference/import.html),
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
Existing Python 3.14 syntax and dependencies are unchanged. Qualification is exact
packet reconstruction <=100000 tokens, import isolation, original integration tests,
actual function CCN/shape and Ruff. No live installation or full-task token saving
is inferred from this module alone.

## Canonical structure contract — 2026-11-03

Capture byte policies belong to [reliability-defaults](reliability-defaults.md),
with unchanged legacy aliases. [breadcrumb-protocol](breadcrumb-protocol.md)
owns complete linked-record syntax and its schemas; storage and execution remain separate.

## Complete bundle value — 2026-11-04

The existing frozen BreadcrumbBundle dataclass belongs to this wire protocol:
manifest, anchor, parts and content retain their exact field types/order and
constructor semantics. breadcrumb_storage keeps the identical class alias and
its legacy __module__ remains breadcrumb_storage. Protocol construction does not
import storage; pickle can load the old facade and is not claimed to be isolated.
Baseline protocol-4/5 bytes, frozen values and facade identity are qualified.
BreadcrumbPublication and handler dispatch version remain storage concerns.
