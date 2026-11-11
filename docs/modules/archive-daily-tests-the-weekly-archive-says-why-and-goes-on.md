# Tests of `scripts/archive_daily.py`

- `tests/test_the_weekly_archive_says_why_and_goes_on.py`: The weekly archive names why an old day stays flat, and one failure does not end it.

`scripts/archive_daily.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_weekly_archive_says_why_and_goes_on.py -q`.
