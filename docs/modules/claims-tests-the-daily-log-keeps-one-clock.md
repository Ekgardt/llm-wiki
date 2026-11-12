# Tests of `scripts/claims.py`

- `tests/test_the_daily_log_keeps_one_clock.py`: The daily log keeps one clock, and a claim names the instant its block meant.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_daily_log_keeps_one_clock.py -q`.
