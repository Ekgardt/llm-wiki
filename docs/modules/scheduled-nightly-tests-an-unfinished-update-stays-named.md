# Tests of `scripts/scheduled_nightly.py`

- `tests/test_an_unfinished_update_stays_named.py`: What an earlier update left undone stays named until it is done (audit 2026-09-26 B-24).

`scripts/scheduled_nightly.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unfinished_update_stays_named.py -q`.
