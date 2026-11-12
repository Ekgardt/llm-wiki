# Tests of `scripts/memory_state.py`

- `tests/test_four_small_capture_corrections.py`: Bounded maps in the state file, and a stray byte that no longer loses a session.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_four_small_capture_corrections.py -q`.
