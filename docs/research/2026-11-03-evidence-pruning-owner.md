# Qualify byte-exact sentence pruning in place

Date: 2026-11-03; internal responsibility under the approved decomposition plan.

`scripts/evidence_pruning.py` already accepts source bytes, a range, the question
and an encoder callable. It returns byte spans and imports no product module.
`query_memory.py` owns its deadline, source selection, context budget, citations
and final evidence. Preserve those owners and both production files' behavior.

Qualify the current pruning source in place. Move four original unit regressions,
source bytes and their numerical encoder fixture from the mixed reply test into
the packet. Keep its real context/hash integration in the original file, importing
the exact shared fixture, and keep the clock/base-install suites. Preserve all
original definition ASTs and production hashes. The integration files remain
explicit whole-project ownership work, not omitted qualification requirements.

Keep the existing turn recognition, byte thresholds, first-sentence retention,
minimum selected sentence count, stable score ties and adjacent-span merging.
The original September8 research remains full, dated history; no claim of new
retrieval quality or end-to-end token savings follows from relocating tests.

Before suite for encoder/pruning:29passed. Evidence:
logs/encoder-pruning-before.xml and logs/encoder-pruning-before-ast.json.
Use the fresh builtin graph before implementation, supplementing partial callers
with query_memory.py source. Verify full local source closure, checked reads,
isolated imports, exact artifact reconstruction, budgets and actual CCN/shape.

Alternatives: couple pruning to query_memory's state/generation closure; replace
existing integration with fake-only acceptance; alter pruning to meet a source
budget. All are unnecessary: source remains unchanged and the current pure
responsibility already has a useful independent contract. No new dependency,
limit, model call, installation or semantic compression algorithm.

Three independent primary publishers checked2026-11-03:
- NumPy argsort: https://numpy.org/doc/stable/reference/generated/numpy.argsort.html
  — stable ordering preserves original order for tied scores.
- Python regular expressions: https://docs.python.org/3.14/library/re.html —
  match start/end indices select the original input span; preserve byte-pattern
  matching and exact source slicing instead of rebuilding sentence text.
- Fowler bounded context: https://martinfowler.com/bliki/BoundedContext.html —
  explicit model responsibilities support keeping byte-span selection separate
  from evidence publication. The principle alone does not prove retrieval quality.

Builtin graph checked before implementation: generation18e48e8dc525ca0c-ee4904d1, abade77e. load_encoder2callers, cached_file3, pruned_spans3, prunes8, sentence_spans3. Partial graph supplemented by search_memory/query_memory source. Evidence: logs/encoder-pruning-graph.json.
