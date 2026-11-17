# Tests of `scripts/reliable_memory.py`

- `tests/one_bad_day_does_not_hold_the_rest_helpers.py`: Helpers shared by test_one_bad_day_does_not_hold_the_rest.py and the test files importing them.

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/one_bad_day_does_not_hold_the_rest_helpers.py -q`.
