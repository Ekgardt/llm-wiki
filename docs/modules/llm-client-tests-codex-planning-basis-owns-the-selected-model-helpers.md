# Tests of `scripts/llm_client.py`

- `tests/codex_planning_basis_owns_the_selected_model_helpers.py`: Helpers shared by test_codex_planning_basis_owns_the_selected_model.py and the test files importing them.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/codex_planning_basis_owns_the_selected_model_helpers.py -q`.
