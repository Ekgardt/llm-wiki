# Tests of `scripts/memory_state.py`

- `tests/test_every_store_has_a_bound.py`: Every store that only grew has an owner that bounds it.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_store_has_a_bound.py -q`.
