# Tests of `scripts/reliable_memory.py`

- `tests/llm_descriptors_helpers.py`: Helpers shared by test_llm_descriptors.py and the test files importing them.

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/llm_descriptors_helpers.py -q`.
