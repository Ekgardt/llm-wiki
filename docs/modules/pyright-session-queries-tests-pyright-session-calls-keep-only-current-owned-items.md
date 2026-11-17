# Tests of `scripts/llm_wiki/pyright_session_queries.py`

- `tests/test_pyright_session_calls_keep_only_current_owned_items.py`

`scripts/llm_wiki/pyright_session_queries.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pyright_session_calls_keep_only_current_owned_items.py -q`.
