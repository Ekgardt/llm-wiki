# Tests of `scripts/query_memory.py`

- `tests/test_the_context_is_built_under_the_same_clock.py`: Building the context obeys the answer's deadline, says what it shed, and prunes the first turn.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_context_is_built_under_the_same_clock.py -q`.
