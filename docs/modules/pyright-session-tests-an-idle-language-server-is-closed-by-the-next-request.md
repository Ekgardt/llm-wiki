# Tests filed with `scripts/pyright_session.py`

- `tests/test_an_idle_language_server_is_closed_by_the_next_request.py`: A server nobody has used for five minutes is closed by the next request.

These files do not import `scripts/pyright_session.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/pyright_session_manager.py`: its exact derived description.
- `scripts/pyright_profile.py`: its exact derived description.
- `scripts/repository_scope.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_idle_language_server_is_closed_by_the_next_request.py -q`.
