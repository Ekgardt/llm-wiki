# Tests filed with `scripts/memory_state.py`

- `tests/test_structure.py`: Structural invariants for the llm-wiki repository.

These files do not import `scripts/memory_state.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/runtime_roots.py`: its exact derived description.
- `scripts/lsp_paths.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_structure.py -q`.
