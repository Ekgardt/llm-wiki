# Tests of `scripts/queue_core.py`

- `tests/queue_v3_corruption_helpers.py`: Helpers shared by test_queue_v3_corruption.py and the test files importing them.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/queue_v3_corruption_helpers.py -q`.
