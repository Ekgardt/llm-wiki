# Tests of `scripts/query_memory.py`

- `tests/test_a_count_that_reached_the_edge_looks_again.py`: An answer that counted what it was shown gets one more look, and only one.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_count_that_reached_the_edge_looks_again.py -q`.
