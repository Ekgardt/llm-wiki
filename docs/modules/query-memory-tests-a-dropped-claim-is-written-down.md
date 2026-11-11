# Tests of `scripts/query_memory.py`

- `tests/test_a_dropped_claim_is_written_down.py`: A gate that drops a claim must say so, even when the answer still goes out.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dropped_claim_is_written_down.py -q`.
