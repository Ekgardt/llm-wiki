# Tests of `scripts/reflection.py`

- `tests/test_reflection.py`: Tests for reflection.py — A-MEM memory evolution (page consolidation).

`scripts/reflection.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_reflection.py -q`.
