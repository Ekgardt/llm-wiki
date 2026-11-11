# Tests of `scripts/llm_client.py`

- `tests/the_compile_decides_what_the_snapshot_already_knows_helpers.py`: Helpers shared by test_the_compile_decides_what_the_snapshot_already_knows.py and the test files importing them.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/the_compile_decides_what_the_snapshot_already_knows_helpers.py -q`.
