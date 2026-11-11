# Tests of `scripts/retrieval.py`

- `tests/test_a_missing_leg_is_reported.py`: A leg the run asked for and did not get is reported missing (audit 2026-09-26 C-10).

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_missing_leg_is_reported.py -q`.
