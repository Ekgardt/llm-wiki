# Tests of `scripts/search_memory.py`

- `tests/test_vector_freshness_parity.py`: Task 12: vector freshness, Lance distance conversion, filter parity.

`scripts/search_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vector_freshness_parity.py -q`.
