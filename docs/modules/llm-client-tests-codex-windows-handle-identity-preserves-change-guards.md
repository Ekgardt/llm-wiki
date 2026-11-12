# Tests of `scripts/llm_client.py`

- `tests/test_codex_windows_handle_identity_preserves_change_guards.py`: Handle identity keeps Windows timestamp meanings and file-change guards.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_windows_handle_identity_preserves_change_guards.py -q`.
