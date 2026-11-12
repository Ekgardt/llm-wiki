# Tests of `scripts/code_graph.py`

- `tests/test_summary_listings_state_their_bound.py`: Two lists in the summary were unbounded and said nothing about it.

`scripts/code_graph.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_summary_listings_state_their_bound.py -q`.
