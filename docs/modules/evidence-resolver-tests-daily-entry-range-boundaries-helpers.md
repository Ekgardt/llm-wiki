# Tests of `scripts/evidence_resolver.py`

- `tests/daily_entry_range_boundaries_helpers.py`: Helpers shared by test_daily_entry_range_boundaries.py and the test files importing them.

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/daily_entry_range_boundaries_helpers.py -q`.
