# Tests of `scripts/queue_core.py`

- `tests/test_queue_durable_file_conflicts.py`: A pre-existing result is compared through the same safe reader as recovery.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_queue_durable_file_conflicts.py -q`.
