# Tests of `scripts/llm_wiki/pyright_session_manager.py`

- `tests/test_language_server_session_manager_refuses_what_it_cannot_hold.py`

`scripts/llm_wiki/pyright_session_manager.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_language_server_session_manager_refuses_what_it_cannot_hold.py -q`.
