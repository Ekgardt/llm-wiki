# Qualify the existing local ONNX encoder in place

Date: 2026-11-03; internal responsibility under the approved decomposition plan.

`scripts/onnx_encoder.py` already has no local product imports. It owns cached
model/tokenizer lookup, CPU session creation, token padding, masked mean pooling,
normalization and stable output row placement. The search caller owns selection,
missing-model diagnostics and fallback. Qualify this boundary without changing
production bytes, model revision, dependencies, thresholds or loader behavior.

Move four original unit tests and all their fixtures from
`test_the_encoder_runs_without_torch.py` to its own packet. Keep both whole-product
policy tests there: no product import of sentence_transformers and no torch in
search extras. Preserve every original definition AST. Keep and run the separate
missing-embedder and local-cache query integrations; these remain explicit future
integration ownership obligations, not a source-closure exception. Retain the
whole September25 measurement as historical evidence, not a new benchmark result.

Before suite for both encoder/pruning boundaries:29passed. Original source hashes
and all22test/helper/class definitions are captured in
logs/encoder-pruning-before-ast.json. Fresh builtin graph plus source search must
confirm search_memory._get_embedder is the loader consumer before implementation.

Alternatives: moving production creates needless aliases; combining encoding with
search couples numerical processing to generation/state policy; claiming mocks
prove real-model quality is invalid. Use unchanged original tests, import guards,
whole-packet byte reconstruction and token limits, real integration regression and
actual CCN/shape measurement. No new runtime, network fetch or live installation.

Three independent primary publishers checked2026-11-03:
- ONNX Runtime API: https://onnxruntime.ai/docs/api/python/api_summary.html —
  session feeds and explicit CPU provider remain the existing interface.
- Hugging Face cache API:
  https://huggingface.co/docs/huggingface_hub/package_reference/cache — exact
  revision lookup returns a cached path or an absence sentinel; preserve local-only
  lookup and the wrapper's FileNotFoundError for non-string results.
- NumPy sorting/reference: https://numpy.org/doc/stable/reference/routines.sort.html
  — array indices and row ordering matter; no NumPy/API upgrade is part of this move.

Builtin graph checked before implementation: generation18e48e8dc525ca0c-ee4904d1, abade77e. load_encoder2callers, cached_file3, pruned_spans3, prunes8, sentence_spans3. Partial graph supplemented by search_memory/query_memory source. Evidence: logs/encoder-pruning-graph.json.
