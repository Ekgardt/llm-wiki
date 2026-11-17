# Tests of `scripts/flush_memory.py`

- `tests/test_capture_worker_reclaims_leases.py`: The capture worker retries a capture whose previous worker died.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_worker_reclaims_leases.py -q`.
