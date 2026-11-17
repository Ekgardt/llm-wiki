# Tests filed with `scripts/pyright_session.py`

- `tests/test_a_stranded_session_close_is_retried_by_the_next_caller.py`: A session whose close failed is closed again by the next caller that meets it.

These files do not import `scripts/pyright_session.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/lsp_process_state.py`: its exact derived description.
- `scripts/llm_wiki/pyright_session_manager.py`: its exact derived description.
- `scripts/pyright_profile.py`: its exact derived description.
- `scripts/repository_scope.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_stranded_session_close_is_retried_by_the_next_caller.py -q`.
