# Tests of `scripts/llm_wiki/restricted_paths.py`

- `tests/test_runtime_policy_compatibility.py`: Existing runtime policy imports keep the same function objects.

`scripts/llm_wiki/restricted_paths.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_runtime_policy_compatibility.py -q`.
