# Tests of `scripts/claims.py`

- `tests/test_lock_writes_are_complete.py`: Small lock records still require complete writes and owned descriptor cleanup.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lock_writes_are_complete.py -q`.
