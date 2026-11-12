# Tests of `scripts/evidence_resolver.py`

- `tests/test_daily_entry_range_boundaries.py`: The canonical boundary grammar must work without a whole-file buffer.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_daily_entry_range_boundaries.py -q`.
