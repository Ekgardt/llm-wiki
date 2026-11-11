# Tests of `scripts/generation_catalog.py`

- `tests/generation_catalog_helpers.py`: Helpers shared by test_generation_catalog.py and the test files importing them.

`scripts/generation_catalog.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/generation_catalog_helpers.py -q`.
