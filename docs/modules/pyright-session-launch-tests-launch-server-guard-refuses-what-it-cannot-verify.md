# Tests of `scripts/llm_wiki/pyright_session_launch.py`

- `tests/test_launch_server_guard_refuses_what_it_cannot_verify.py`

`scripts/llm_wiki/pyright_session_launch.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_launch_server_guard_refuses_what_it_cannot_verify.py -q`.
