# Tests of `scripts/llm_client.py`

- `tests/test_compile_refresh_requalifies_changed_provider_environment.py`: A fresh batch must not reuse pre-reranker process environment evidence.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_refresh_requalifies_changed_provider_environment.py -q`.
