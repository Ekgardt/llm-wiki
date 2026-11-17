# Tests filed with `scripts/memory_state.py`

- `tests/test_audit_fixes.py`: Regression tests for audit-critical path contracts.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/compile_pipeline.py`: its exact derived description.
- `scripts/llm_client.py`: its exact derived description.
- `scripts/llm_wiki/compile_pipeline_base.py`: its exact derived description.
- `scripts/llm_wiki/compile_pipeline_selection.py`: its exact derived description.
- `scripts/llm_wiki/runtime_roots.py`: its exact derived description.
- `scripts/markdown_transaction.py`: its exact derived description.
- `scripts/queue_core.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_audit_fixes.py -q`.
