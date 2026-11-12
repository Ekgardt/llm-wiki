# Tests of `scripts/installed_memory_repair.py`

- `tests/test_reliability_v3_adoption.py`

`scripts/installed_memory_repair.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_reliability_v3_adoption.py -q`.
