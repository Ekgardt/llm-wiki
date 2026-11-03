# Shared session classification protocol

Owns scripts/llm_wiki/classification_protocol.py: the existing classification
system prompt and prompt renderer, tier constants, decoration normalization,
legacy sentinel recognition and response splitting. This leaf imports no product
runtime, provider or IO component. flush_memory keeps direct aliases for existing
consumers and monkeypatch points; the benchmark imports this same protocol
implementation directly instead of loading the capture executor to score a reply.

All prompt bytes, parsing branches, sentinel values and fallback behavior are
preserved. Recognized tiers return lowercase major/minor/ok; undecorated content
without a tier defaults to minor, while empty input, legacy no-content forms and
the existing non-flush prefixes return ok. A recognized first-line FLUSH_OK with
following text retains that body in the parser result; the owner guide states
that existing behavior rather than silently tightening it. The old function's
misleading return-value documentation is corrected without changing executable
statements. Queue and capture admission/writes remain untouched.

The existing parser test path stays available with the same assertions, importing
the leaf directly. Its unused helpers and stale claims about retired compile
trigger tests are removed. Separate integration assertions retain old alias
identity, real capture-prompt equality and queue/redrive monkeypatch behavior.
The packet retains the complete September17/Sep23 rationale, protocol tests,
prompt baseline bytes and actual bootstrap. No source omission or new limits.
Aliases may be removed only after supported old-path consumers migrate; the
queue still imports the old name, so that condition is not met.

## Research and boundary decision — 2026-11-03

The common prompt and parser define one protocol. Splitting them risks the stand
measuring a different prompt, while retaining them in capture forces unrelated
runtime imports into pure scoring. The approved autonomous decomposition plan
covers this internal leaf; no runtime location, installation, dependency, data
schema or provider behavior changes. Existing source/callers and tests identify
capture, deferred queue flush, and the benchmark as consumers. Dynamic graph
edges are supplemented by source and monkeypatch-use inspection.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
and [Python string operations](https://docs.python.org/3.14/library/stdtypes.html#str.strip).
The sources inform responsibility boundaries and faithful parsing tests, not a
new classification-quality claim. Actual CCN/shape <=5, exact reconstruction
within100000 o200k_base tokens, consumer tests and native CI remain required.

## Canonical structure contract — 2026-11-03

The [classification protocol](classification-protocol.md) owns the shared
prompt and response grammar independently of capture runtime. flush_memory keeps
old-path aliases; the benchmark imports the same leaf. Prompt bytes, parser
behavior, queue admission and publication contracts stay unchanged.
