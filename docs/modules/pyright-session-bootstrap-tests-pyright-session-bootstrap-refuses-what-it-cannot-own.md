# Tests of `scripts/llm_wiki/pyright_session_bootstrap.py`

- `tests/test_pyright_session_bootstrap_refuses_what_it_cannot_own.py`

`scripts/llm_wiki/pyright_session_bootstrap.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pyright_session_bootstrap_refuses_what_it_cannot_own.py -q`.
