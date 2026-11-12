# Tests of `scripts/memory_state.py`

- `tests/a_corrupt_state_is_not_replaced_by_an_empty_one_helpers.py`: Helpers shared by test_a_corrupt_state_is_not_replaced_by_an_empty_one.py and the test files importing them.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_corrupt_state_is_not_replaced_by_an_empty_one_helpers.py -q`.
