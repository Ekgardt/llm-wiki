# Tests of `scripts/llm_wiki/runtime_contracts.py`

- `tests/test_runtime_io_compatibility.py`: Runtime IO public names retain object identity and their serialized paths.

`scripts/llm_wiki/runtime_contracts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_runtime_io_compatibility.py -q`.
