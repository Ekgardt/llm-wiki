# Tests of `scripts/markdown_transaction.py`

- `tests/a_dead_owner_is_reclaimed_without_its_marker_helpers.py`: Helpers shared by test_a_dead_owner_is_reclaimed_without_its_marker.py and the test files importing them.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_dead_owner_is_reclaimed_without_its_marker_helpers.py -q`.
