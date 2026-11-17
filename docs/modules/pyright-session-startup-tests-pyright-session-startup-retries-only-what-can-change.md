# Tests of `scripts/llm_wiki/pyright_session_startup.py`

- `tests/test_pyright_session_startup_retries_only_what_can_change.py`

`scripts/llm_wiki/pyright_session_startup.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pyright_session_startup_retries_only_what_can_change.py -q`.
