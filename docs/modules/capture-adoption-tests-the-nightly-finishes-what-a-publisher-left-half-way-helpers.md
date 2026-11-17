# Tests filed with `scripts/capture_adoption.py`

- `tests/the_nightly_finishes_what_a_publisher_left_half_way_helpers.py`: Helpers shared by test_the_nightly_finishes_what_a_publisher_left_half_way.py and the test files importing them.

These files do not import `scripts/capture_adoption.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/markdown_transaction.py`: its exact derived description.
- `scripts/queue_core.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/the_nightly_finishes_what_a_publisher_left_half_way_helpers.py -q`.
