# Tests of `scripts/queue_core.py`

- `tests/test_queue_lease_input_binding.py`: A valid owner token cannot authorize different task input.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_queue_lease_input_binding.py -q`.
