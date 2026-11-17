# Tests of `scripts/archive_daily.py`

- `tests/archive_daily_bagit_helpers.py`: Helpers shared by test_archive_daily_bagit.py and the test files importing them.

`scripts/archive_daily.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/archive_daily_bagit_helpers.py -q`.
