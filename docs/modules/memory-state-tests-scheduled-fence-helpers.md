# Tests of `scripts/memory_state.py`

- `tests/scheduled_fence_helpers.py`: Helpers shared by test_scheduled_fence.py and the test files importing them.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/scheduled_fence_helpers.py -q`.
