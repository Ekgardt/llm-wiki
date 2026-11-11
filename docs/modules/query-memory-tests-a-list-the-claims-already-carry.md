# Tests of `scripts/query_memory.py`

- `tests/test_a_list_the_claims_already_carry.py`: A reply is refused for what it asserts, not for a list beside its claims.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_list_the_claims_already_carry.py -q`.
