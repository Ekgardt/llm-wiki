# Tests of `scripts/flush_memory.py`

- `tests/test_a_redriven_capture_meets_no_leftovers.py`: A redriven capture meets no leftover decision, and its family does not stop the purge.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_redriven_capture_meets_no_leftovers.py -q`.
