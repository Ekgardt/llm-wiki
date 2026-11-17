# Tests of `scripts/llm_client.py`

- `tests/test_codex_planning_basis_owns_the_selected_model.py`: An attempt retains its actual model and executable without changing defaults.

`scripts/llm_client.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_planning_basis_owns_the_selected_model.py -q`.
