# Tests of `scripts/archive_daily.py`

- `tests/test_archive_receipt_authority_keeps_one_clock.py`: Archive receipt authority must not replace an expired clock with invalid evidence.

`scripts/archive_daily.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_archive_receipt_authority_keeps_one_clock.py -q`.
