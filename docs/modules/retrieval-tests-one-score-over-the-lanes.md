# Tests of `scripts/retrieval.py`

- `tests/test_one_score_over_the_lanes.py`: A turn one lane never returned still wins its place, and a page's order is untouched.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_score_over_the_lanes.py -q`.
