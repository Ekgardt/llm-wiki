# Permanent breadcrumb document format

llm_wiki.breadcrumb_documents owns the existing canonical Markdown representation
of complete breadcrumb sources: occurrence-derived paths, archive names, head and
part rendering, embedded record extraction and exact reconstruction through a
caller-supplied reader. It shares breadcrumb_protocol and the existing page bound.
No function body, marker, frontmatter, schema version or digest algorithm changes.
The reader callback must return contained, stable, bounded Markdown bytes; the
codec validates every requested document and its regenerated path and encoding.

breadcrumb_evidence keeps filesystem containment, deadlines, archive fallback,
transaction retries, DLP and publication. It reexports the same codec objects for
capture, compile, fact and archive callers. Its read/restore instrumentation points
remain at their existing lookup sites. A rendered source or complete chain alone
does not grant publication, terminal disposition, queue completion or cleanup.
Compatibility aliases remain until those consumers deliberately migrate.

The packet retains full codec, protocol and schemas, bounded IO and its source
closure, formats, policy and all applicable guides. Original pure evidence tests
move intact with their single shared factory; runtime integrations import that
factory and preserve their original assertions. Byte snapshots from the original
renderer cover complete Unicode/Markdown-like inputs, links and embedded records.
Runtime integration separately verifies mixed archive/current locations, caller
budgets, transaction refusal/retry, storage, capture and historical readers.

## Boundary research — 2026-11-04

Sources read on November 3, immediately before this boundary decision:
[bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[Python imports](https://docs.python.org/3.14/reference/import.html),
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
Separate the representation contract from publishing effects; keep one cohesive
codec instead of per-helper wrappers. Whole dependencies and required history
remain source context, not substitutes describing an omitted implementation.
Python 3.14 and existing dependencies remain unchanged. Acceptance requires full
source closure, isolated import, actual CCN/shape, Ruff, original integration and
exact packet reconstruction within 100000 tokens. This alone establishes neither
project completion nor full-cycle LLM savings. No live installation is authorized.
