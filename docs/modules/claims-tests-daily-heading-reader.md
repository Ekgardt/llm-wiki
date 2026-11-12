# Tests of `scripts/claims.py`

- `tests/test_daily_heading_reader.py`: NEW-120: the claim reader accepts the heading this vault actually writes.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_daily_heading_reader.py -q`.
