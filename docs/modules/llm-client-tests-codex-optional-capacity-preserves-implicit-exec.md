# Tests of `scripts/llm_client.py`

- `tests/test_codex_optional_capacity_preserves_implicit_exec.py`: Missing optional discovery cannot disable an otherwise supported CLI.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_optional_capacity_preserves_implicit_exec.py -q`.
