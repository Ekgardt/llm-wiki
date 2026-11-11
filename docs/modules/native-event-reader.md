# Native event reader

The existing event_envelope module owns canonical native-frame selection and
reading, including complete breadcrumb-part content and ancestry-based dispatch.
Its sixteen reader functions stay byte-identical. Constructor exports remain
direct aliases of event_envelope_values, and that complete implementation stays
in this packet with secret_redact, breadcrumb_protocol, its schemas, formats and
policy. No provider, queue, collector, source-authority or runtime IO is imported.

Generic captured objects do not become native user facts. Native-shaped objects
must satisfy the same version, exact fields, canonical JSON, typed payload and
source-string checks. Incomplete native JSON retains its distinct error; explicit
fragment allowance retains its existing behavior. Part reading retains the
protocol's integrity checks and first-part rule. The parser does not prove a
file's physical citation or authorize corpus membership: fact_keys/corpus_snapshot
and their integration tests retain that responsibility.

Six existing decoder test functions move without changing their assertions or
fixtures; the single _frame helper moves with its original AST from the physical
citation tests, which retain an alias at the old helper path. Source-authority
and constructor facade integration remain in their original test file. No tests
are dropped or excluded from the full regression. Runtime publication and generic
journal/archive scenarios remain integration obligations of the whole project.

The complete history lives in [Breadcrumb history](breadcrumb-history.md). This
packet retains exact original records for overview/status, the proposed contract,
identity/time, compatibility/limits, linked-record size correction and the full
generic/native identity correction. Those records explain the format and error
boundary; retrieval scoring, lease shutdown and publication IO are separate owners.
The full catalog validates all original bytes, including unselected records.

## Research — 2026-11-04

[JSON interchange](https://www.rfc-editor.org/info/rfc8259/),
[Python JSON](https://docs.python.org/3.14/library/json.html) and
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
were checked before qualification. Parsed outer identity and unchanged canonical
validation preserve the existing generic/native distinction. A new JSON library,
rewriting physical evidence or splitting helpers individually adds no needed
capability. This is ownership qualification in place, not a format migration.
Full source context, byte-exact reconstruction, <=100000 tokens, isolated imports,
actual complexity, original integration and measured test weights remain gates.

## Canonical structural contract

[Native event reader](native-event-reader.md) owns the existing physical
native-frame decoder in `event_envelope`; constructor aliases and source-authority
integration keep their existing behavior and public paths.
