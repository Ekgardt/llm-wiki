# Tests filed with `scripts/memory_state.py`

- `tests/test_compile_failure.py`: Regression test: compile_memory fail-safe semantics (Round 1 #C2).

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/compile_pipeline.py`: its exact derived description.
- `scripts/llm_wiki/compile_pipeline_selection.py`: its exact derived description.
- `scripts/queue_core.py`: its exact derived description.
- `scripts/reliable_memory.py`: its exact derived description.
- `scripts/settings.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_failure.py -q`.
