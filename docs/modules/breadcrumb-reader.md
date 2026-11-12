# Permanent breadcrumb source reader

llm_wiki.breadcrumb_reader owns the existing readonly source-path validation,
stable document reading, archive fallback and complete-source read under one
optional caller deadline. The vault is explicit; the module neither resolves a
live runtime root nor imports queues, providers or transaction publication.
Every original function body and error remains unchanged. The same bounded_io
and breadcrumb_documents owners provide byte reads, clocks and integrity checks.

A relative session-evidence path is checked against the resolved vault. Missing
current evidence may be read at its canonical archive path; other read failures
are not masked as archive misses. All parts and archive attempts share the original
deadline. Final reconstruction is checked before returning. Checks are cooperative:
an individual blocked OS call cannot be preempted. This move adds no stronger
filesystem isolation, new size/time policy or partial-evidence success mode.

breadcrumb_evidence retains publication and direct function aliases for existing
compile, fact, archive and historical readers. Its publication callbacks still
look up those aliases. Readonly tests now instrument the actual reader owner;
compatibility does not promise arbitrary mutation of the old module's internals.
The original five test functions and two helpers move without assertion changes,
and the old shared test-helper paths remain aliases for retrieval integration.

The full packet keeps the document/protocol implementations and schemas, bounded
IO with platform helpers, formats and policy, complete owner guides and relevant
history. Original deadline tests cover expiry before access, between parts, at
final verification, archive reads, invalid budgets and successful complete reads.
Additional path tests exercise ordinary containment refusals, and whole-checkout
publication/retrieval/archive tests retain the old API. New tests receive actual
CI weight measurements before qualification. Full source closure, CCN/shape,
Ruff and exact <=100000 token reconstruction remain gates.

## Boundary research — 2026-11-04

This follows the approved decomposition plan and the preceding document codec.
Keeping IO with publication couples a read-only task to transaction dependencies;
per-helper modules would fragment one deadline/archive contract. Relevant primary
sources checked in this session: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[Python import bindings](https://docs.python.org/3.14/reference/import.html), and
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
No dependency/runtime version, authority file, stored data or live installation
changes. Standalone packet size is not a claim of full-cycle LLM cost savings.

Qualification: all original source/test function ASTs survived the move;
38 initial reader/retrieval checks, 748 registry/context checks and 241 related
breadcrumb/history/native-frame checks passed. The separate payload-report guard
was updated to the already moved document test's new location after CI exposed
its stale dynamic import; its hashes, payloads and name assertions are unchanged.
42 focused checks including that guard passed. Native CI and the full regression
remain separate evidence, not inferred from these focused results.

docs/research/2026-09-23-one-limit-one-place.md is not in this packet: the bounded-io packet owns it,
and this reader consumes bounded_io's reads without defining a read bound.
