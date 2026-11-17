# Tests of `scripts/query_memory.py`

- `tests/test_working_before_the_claims.py`: The model reads before it claims, and the reading never reaches a reader.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_working_before_the_claims.py -q`.
