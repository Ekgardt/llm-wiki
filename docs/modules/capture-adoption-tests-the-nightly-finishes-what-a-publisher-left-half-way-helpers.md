# Tests of `scripts/capture_adoption.py`

- `tests/the_nightly_finishes_what_a_publisher_left_half_way_helpers.py`: Helpers shared by test_the_nightly_finishes_what_a_publisher_left_half_way.py and the test files importing them.

`scripts/capture_adoption.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/the_nightly_finishes_what_a_publisher_left_half_way_helpers.py -q`.
