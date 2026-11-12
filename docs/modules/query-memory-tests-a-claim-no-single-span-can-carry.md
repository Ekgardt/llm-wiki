# Tests of `scripts/query_memory.py`

- `tests/test_a_claim_no_single_span_can_carry.py`: Seven of eight wrong answers needed arithmetic the contract forbade.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_claim_no_single_span_can_carry.py -q`.
