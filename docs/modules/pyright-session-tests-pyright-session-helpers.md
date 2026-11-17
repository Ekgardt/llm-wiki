# Tests filed with `scripts/pyright_session.py`

- `tests/pyright_session_helpers.py`: Helpers shared by the test_pyright_session*.py files and the test files importing them.

These files do not import `scripts/pyright_session.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/pyright_session_core.py`: its exact derived description.
- `scripts/lsp_positions.py`: its exact derived description.
- `scripts/pyright_profile.py`: its exact derived description.
- `scripts/repository_scope.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/pyright_session_helpers.py -q`.
