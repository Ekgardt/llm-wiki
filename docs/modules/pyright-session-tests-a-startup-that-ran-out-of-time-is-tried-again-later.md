# Tests filed with `scripts/pyright_session.py`

- `tests/test_a_startup_that_ran_out_of_time_is_tried_again_later.py`: A start that ran out of time is tried again; a broken install is not.

These files do not import `scripts/pyright_session.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/pyright_session_core.py`: its exact derived description.
- `scripts/llm_wiki/pyright_session_manager.py`: its exact derived description.
- `scripts/llm_wiki/pyright_session_startup.py`: its exact derived description.
- `scripts/lsp_protocol.py`: its exact derived description.
- `scripts/pyright_profile.py`: its exact derived description.
- `scripts/repository_scope.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_startup_that_ran_out_of_time_is_tried_again_later.py -q`.
