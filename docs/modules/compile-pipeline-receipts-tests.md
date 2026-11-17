# Tests of `scripts/llm_wiki/compile_pipeline_receipts.py`

- `tests/evidence_resolver_helpers.py`: Helpers shared by test_evidence_resolver.py and the test files importing them.
- `tests/test_compile_publication_writer_contention.py`: A real competing writer must not discard an already validated compile plan.
- `tests/test_compile_receipt_evidence_preserves_distinct_claims.py`: Receipt records are a set of bindings; semantic assertions remain ordered.

`scripts/llm_wiki/compile_pipeline_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/evidence_resolver_helpers.py tests/test_compile_publication_writer_contention.py tests/test_compile_receipt_evidence_preserves_distinct_claims.py -q`.
