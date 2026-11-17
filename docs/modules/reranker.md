# Cross-encoder reranker for precision search

`scripts/reranker.py` and the tests that exercise them most.

Re-ranks fused candidates using a cross-encoder that scores each (query, document) pair jointly. Runs on CPU via ONNX Runtime when available.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_reranker_that_never_finished.py tests/test_rerank_cancellation_keeps_the_fused_order.py tests/test_reranker.py tests/test_the_reranker_does_not_pay_for_padding.py -q`.
